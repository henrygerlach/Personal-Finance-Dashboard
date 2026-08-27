"""
Classification as the application uses it.

The API is the last resort, not the first step. Every transaction goes through
the same funnel:

1. **The merchant cache.** A merchant somebody already categorised is answered
   from the database. A hand-corrected entry outranks anything the model would
   say, which is what makes a correction in the UI stick.
2. **PayPal recovery.** Comdirect truncates the merchant out of some PayPal
   lines. If a PayPal export has been imported, the payee is looked up there
   by amount and settlement date and put back — after which the line is an
   ordinary transaction naming an ordinary merchant, and steps 1 and 3 handle
   it like any other. See :mod:`app.paypal`.
3. **The unresolvable-PayPal fallback.** A truncated line the export could not
   speak for has nothing in it to classify, so it is filed with the configured
   fallback rather than spending a call on a question with no answer. It is
   marked ``fallback`` and carries confidence 0, so it can be found again —
   the category is knowingly approximate.
4. **The API**, for what is left.

Every result the API returns is then checked twice: the pair must exist in the
hierarchy, and the confidence must clear the threshold. Anything that fails
either check becomes ``Uncategorized / Needs Review`` instead of a category
nobody chose.
"""

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable, Sequence

from app.categories import (
    MAJOR_INCOME,
    NEEDS_REVIEW,
    code_for,
    coerce_pair,
    flow_type_for,
    is_valid_code,
    is_valid_pair,
    pair_for_code,
)
from app.classification.client import (
    ClassificationRequest,
    ClassificationResult,
    ClaudeClassifier,
)
from app.config import get_logger, settings
from app.models import (
    DIRECTION_IN,
    DIRECTION_OUT,
    SOURCE_FALLBACK,
    SOURCE_LLM,
    SOURCE_USER,
    MerchantCategory,
    Transaction,
)
from app.normalization import (
    build_canonical_map,
    is_unresolvable_paypal,
    merchant_key,
    normalize_merchant,
    shorten_merchant,
)
from app.paypal import PayPalLedger, PayPalMatch, has_records, load_ledger

logger = get_logger(__name__)

#: Where an incoming payment at an expense merchant is filed.
REFUND_PAIR = (MAJOR_INCOME, "Refunds & Reimbursements")


def direction_of(amount: float) -> str:
    """Which way one amount ran. Zero counts as outgoing; nothing produces it."""
    return DIRECTION_IN if amount > 0 else DIRECTION_OUT


def opposite(direction: str) -> str:
    """The other direction."""
    return DIRECTION_OUT if direction == DIRECTION_IN else DIRECTION_IN


def entry_applies(major: str, amount: float) -> bool:
    """
    Whether a remembered category can honestly answer an amount of this sign.

    Used when a merchant's only entry was learned from the other direction.
    Most of the time it still answers, which is what keeps one entry per
    merchant enough:

    * A **transfer** means the same thing whichever way it ran — money between
      one's own accounts, or to and from a person — so it answers both.
    * A **credit at an expense merchant** is a refund, and ``refund_target``
      rewrites it. The category is still the right thing to remember, which is
      why the cache is not doubled for it.
    * A **debit at an income merchant** has no honest reading at all. Income
      is positive by nature; a payment *to* the person who pays you
      maintenance is not negative income, and no rule can recover what it
      should have been. That entry must not answer, so the transaction is
      asked about in its own right instead.
    """
    flow = flow_type_for(major)
    if flow == "transfer":
        return True
    if amount > 0:
        return True
    return flow == "expense"


@dataclass
class Classification:
    """The final decision for one transaction."""

    transaction_id: str
    major: str
    minor: str
    flow_type: str
    confidence: float
    reason: str
    source: str
    merchant: str = ""
    #: Set when the merchant came from a PayPal export rather than from the
    #: booking text, so the recovery can be audited and undone.
    paypal_counterparty: str | None = None
    paypal_settlement_code: str | None = None

    @property
    def needs_review(self) -> bool:
        return (self.major, self.minor) == NEEDS_REVIEW


@dataclass
class ClassificationRun:
    """What one pass over a set of transactions did."""

    classifications: list[Classification]
    cache_hits: int = 0
    fallbacks: int = 0
    api_calls: int = 0
    below_threshold: int = 0
    invalid_codes: int = 0
    deduplicated: int = 0
    #: Groups whose only cached answer was learned from the other direction
    #: and could not honestly carry over, so they were asked about instead.
    direction_conflicts: int = 0
    escalated: int = 0
    escalation_resolved: int = 0
    ledgers: list = field(default_factory=list)
    refunds: int = 0
    #: Outgoing amounts that still ended up under an income category. Income
    #: is positive by nature, so these are sent to review rather than counted.
    misfiled_income: int = 0
    failed: int = 0
    paypal_recovered: int = 0
    usage: dict | None = None

    def summary(self) -> str:
        return (
            f"{len(self.classifications)} transactions: "
            f"{self.cache_hits} from cache, {self.api_calls} merchants asked, "
            f"{self.deduplicated} deduplicated, "
            f"{self.paypal_recovered} PayPal payees recovered, "
            f"{self.fallbacks} fallback, {self.failed} failed; "
            f"{self.below_threshold} below threshold, "
            f"{self.invalid_codes} invalid codes -> review; "
            f"{self.refunds} re-filed as refunds, "
            f"{self.direction_conflicts} asked again for their direction, "
            f"{self.misfiled_income} debits under income -> review"
        )


async def lookup_merchant(name: str, direction: str) -> MerchantCategory | None:
    """
    Find a cached category for a normalized merchant name and direction.

    Two fallbacks, in order of how much they give up:

    The name with its trailing token dropped, because card payments append the
    town and direct debits do not. Without that retry the same shop would miss
    the entry its other spelling created.

    Then the other direction, because most merchants only ever run one way and
    would otherwise miss their own entry the first time a refund arrives. What
    that entry may answer is not this function's decision — the caller checks
    it against :func:`entry_applies`, which is where a debit at an income
    merchant is turned away.
    """
    if not name:
        return None

    keys = [merchant_key(name)]
    shorter = shorten_merchant(name)
    if shorter:
        keys.append(merchant_key(shorter))

    for wanted in (direction, opposite(direction)):
        for key in keys:
            entry = await MerchantCategory.find_one(
                MerchantCategory.merchant_key == key,
                MerchantCategory.direction == wanted,
            )
            if entry is not None:
                return entry
    return None


async def remember_merchant(
    name: str,
    major: str,
    minor: str,
    source: str,
    direction: str,
    confidence: float | None = None,
    reason: str | None = None,
    model_version: str | None = None,
) -> None:
    """
    Write a merchant's category to the cache, for one direction.

    A ``user`` entry is never overwritten by the classifier: once a category
    has been corrected by hand it is the answer for that merchant, and a later
    model run must not undo it.

    The direction is part of the identity of the entry, so correcting an
    outgoing payment leaves the answer learned from that merchant's incoming
    ones alone. Without that, one correction to a two-way merchant would undo
    the other direction every time.

    ``model_version`` is recorded so entries written by an older model or
    prompt can be found and re-run later without disturbing hand-made ones.
    """
    if not name or not is_valid_pair(major, minor):
        return

    key = merchant_key(name)
    entry = await MerchantCategory.find_one(
        MerchantCategory.merchant_key == key,
        MerchantCategory.direction == direction,
    )
    now = datetime.utcnow()

    if entry is None:
        await MerchantCategory(
            merchant_key=key,
            display_name=name,
            direction=direction,
            major=major,
            minor=minor,
            category_code=code_for(major, minor),
            source=source,
            confidence=confidence,
            reason=reason,
            hit_count=0,
            first_seen=now,
            last_used=now,
            model_version=model_version,
        ).insert()
        return

    if entry.source == SOURCE_USER and source != SOURCE_USER:
        return

    entry.display_name = name
    entry.major, entry.minor = major, minor
    entry.category_code = code_for(major, minor)
    entry.source = source
    entry.confidence = confidence
    entry.reason = reason
    entry.model_version = model_version
    entry.last_used = now
    await entry.save()


def _review(transaction_id: str, reason: str, merchant: str = "") -> Classification:
    major, minor = NEEDS_REVIEW
    return Classification(
        transaction_id=transaction_id,
        major=major,
        minor=minor,
        flow_type=flow_type_for(major),
        confidence=0.0,
        reason=reason,
        source=SOURCE_LLM,
        merchant=merchant,
    )


class ClassificationService:
    """Runs transactions through cache, fallback rule and API."""

    def __init__(
        self,
        classifier: ClaudeClassifier | None = None,
        threshold: float | None = None,
        escalation_classifier: "ClaudeClassifier | None" = None,
        escalate: bool | None = None,
        paypal_ledger: PayPalLedger | None = None,
    ) -> None:
        self._classifier = classifier
        self._injected_primary = classifier is not None
        self._escalation_classifier = escalation_classifier
        # Loaded on first use and kept for the life of the service: one import
        # classifies in several batches and each would otherwise re-read every
        # stored PayPal row. None means "not looked up yet", not "none exist".
        self._paypal_ledger = paypal_ledger
        self._paypal_ledger_loaded = paypal_ledger is not None
        self.threshold = (
            threshold
            if threshold is not None
            else settings.classification_confidence_threshold
        )
        self.escalate = (
            escalate
            if escalate is not None
            else bool(settings.classification_escalation_model)
        )

    def _get_classifier(self) -> ClaudeClassifier:
        if self._classifier is None:
            self._classifier = ClaudeClassifier()
        return self._classifier

    def _get_escalation_classifier(self) -> ClaudeClassifier | None:
        """
        The stronger model used for a second look, or ``None`` if switched off.

        A separate classifier rather than a parameter, because it keeps its
        own prompt cache and its own ledger — the escalation pass is small and
        its cost should be visible on its own.

        One is only built here when this service also built the primary
        classifier. A caller that injected its own is controlling API access
        deliberately — a test, an evaluation pinned to one model — and must
        not have a second, real client appear behind its back.
        """
        if not self.escalate:
            return None
        if self._escalation_classifier is not None:
            return self._escalation_classifier
        if self._injected_primary:
            return None
        model = settings.classification_escalation_model
        if not model or model == self._get_classifier().model:
            return None
        self._escalation_classifier = ClaudeClassifier(model=model)
        return self._escalation_classifier

    async def _get_paypal_ledger(self) -> PayPalLedger | None:
        """
        The PayPal ledger, or None when no export has been imported.

        Loaded lazily so an installation that never imports a PayPal export
        pays nothing for the feature, and cached so a multi-batch import reads
        the collection once.
        """
        if self._paypal_ledger_loaded:
            return self._paypal_ledger
        self._paypal_ledger_loaded = True
        try:
            if await has_records():
                self._paypal_ledger = await load_ledger()
        except Exception:  # pragma: no cover - the feature is optional
            # A missing collection or an unreachable database must not take an
            # import down: without a ledger the fallback still applies, which
            # is exactly the behaviour this feature replaces.
            logger.warning("PayPal ledger unavailable; falling back", exc_info=True)
            self._paypal_ledger = None
        return self._paypal_ledger

    async def _recover_paypal_payees(
        self, transactions: Sequence[Transaction]
    ) -> dict[str, PayPalMatch]:
        """
        Look up the payee of every truncated PayPal line, by transaction id.

        Only lines Comdirect actually truncated are looked up. A PayPal line
        that already names its merchant is left alone: the booking text is the
        record the bank stands behind, and the export is only consulted where
        the bank has nothing to say.
        """
        truncated = [t for t in transactions if is_unresolvable_paypal(t.booking_text)]
        if not truncated:
            return {}

        ledger = await self._get_paypal_ledger()
        if ledger is None:
            return {}

        matches: dict[str, PayPalMatch] = {}
        for transaction in truncated:
            match = ledger.resolve(transaction.amount, transaction.date.date())
            if match is not None:
                matches[str(transaction.id)] = match
        return matches

    async def classify(
        self, transactions: Sequence[Transaction], use_cache: bool = True
    ) -> ClassificationRun:
        """
        Classify transactions, consulting the cache before the API.

        The API is the last step, not the first. Each transaction is reduced
        to a merchant, merchants that appear more than once in the batch are
        collapsed onto one, and only merchants nobody has seen before are sent
        on. A monthly import of 150 transactions turns into a handful of
        merchants and one request.

        Args:
            transactions: The transactions to classify. Not saved here — the
                caller decides whether a result is written.
            use_cache: Set to False to send everything to the API. The
                evaluation needs this: answering from a cache built out of the
                same data it is scoring would measure nothing.
        """
        run = ClassificationRun(classifications=[])

        # Step 1: reduce every transaction to a merchant, then fold the
        # spellings of one merchant together within this batch.
        raw_names = {
            str(t.id): normalize_merchant(t.booking_text, t.process)
            for t in transactions
        }

        # Step 1b: put back the payees Comdirect truncated out of PayPal
        # lines. This happens before the names are folded together, so a payee
        # recovered here merges with the same merchant seen under its own name
        # elsewhere in the batch and is answered from the cache with it.
        paypal_matches = await self._recover_paypal_payees(transactions)
        for transaction_id, match in paypal_matches.items():
            raw_names[transaction_id] = normalize_merchant(match.counterparty)
        run.paypal_recovered = len(paypal_matches)

        canonical = build_canonical_map(name for name in raw_names.values() if name)
        merchants = {
            transaction_id: canonical.get(name, name)
            for transaction_id, name in raw_names.items()
        }

        # Step 2: group the transactions by merchant key. Everything decided
        # for one key applies to every transaction carrying it.
        by_key: dict[str, list[Transaction]] = defaultdict(list)
        for transaction in transactions:
            transaction_id = str(transaction.id)

            if (
                is_unresolvable_paypal(transaction.booking_text)
                and transaction_id not in paypal_matches
            ):
                run.fallbacks += 1
                run.classifications.append(self._paypal_fallback(transaction_id))
                continue

            name = merchants[transaction_id]
            # A line with no merchant at all cannot be grouped with anything,
            # so it keeps its own bucket and is asked about on its own.
            #
            # The direction is part of the group key. One answer is fanned out
            # over the whole group, and a merchant can produce both directions
            # — a purchase at Amazon and the refund for it. Grouping on the
            # merchant alone let the first transaction's sign decide for the
            # rest, which filed outgoing payments as refunds. Only the twelve
            # merchants that actually run both ways cost a second request.
            direction = direction_of(transaction.amount)
            key = f"{merchant_key(name)}/{direction}" if name else f"\0{transaction_id}"
            by_key[key].append(transaction)

        # Step 3: the cache answers what it knows, per merchant rather than
        # per transaction.
        to_ask: list[ClassificationRequest] = []
        for key, group in by_key.items():
            representative = group[0]
            name = merchants[str(representative.id)]

            if use_cache:
                entry = await lookup_merchant(name, direction_of(representative.amount))
                # An entry learned from the other direction does not always
                # carry over. Where it cannot, the group is asked about in its
                # own right and the answer is remembered for *its* direction,
                # so the merchant ends up with one entry per meaning.
                if entry is not None and not entry_applies(
                    entry.major, representative.amount
                ):
                    run.direction_conflicts += 1
                    entry = None
                if entry is not None:
                    entry.hit_count += len(group)
                    entry.last_used = datetime.utcnow()
                    await entry.save()
                    for transaction in group:
                        run.cache_hits += 1
                        run.classifications.append(
                            Classification(
                                transaction_id=str(transaction.id),
                                major=entry.major,
                                minor=entry.minor,
                                flow_type=flow_type_for(entry.major),
                                confidence=1.0 if entry.source == SOURCE_USER else 0.9,
                                reason=f"Known merchant ({entry.source}).",
                                source=entry.source,
                                merchant=name,
                            )
                        )
                    continue

            # Step 4: one request per unknown merchant, not per transaction.
            run.deduplicated += len(group) - 1
            to_ask.append(
                ClassificationRequest(
                    id=key,
                    text=name or representative.booking_text[:120],
                    amount=representative.amount,
                    process=representative.process,
                )
            )

        if to_ask:
            await self._ask_api(to_ask, by_key, merchants, run)
            # Step 5: everything the first model was unsure about gets a
            # second look from a stronger one, under the same rules. It runs
            # as its own pass rather than a retry inside the batch so its cost
            # and its effect stay separately visible.
            await self._escalate(to_ask, by_key, merchants, run)

        # Applied once, at the end, so it covers cache hits, API answers and
        # the PayPal fallback alike rather than being repeated per branch.
        # This is also where the per-merchant answer is corrected for the one
        # transaction that ran the other way.
        amounts = {str(t.id): t.amount for t in transactions}
        refiled = []
        for classification in run.classifications:
            amount = amounts.get(classification.transaction_id, 0.0)
            adjusted = apply_refund_rule(classification, amount)
            if adjusted is not classification:
                run.refunds += 1
            # The other half of the same disagreement, and the last line of
            # defence: a debit that still carries an income category, whether
            # the model chose it or a cache entry survived entry_applies.
            # Unlike a refund it has no derivable target, so it goes to review
            # rather than being counted as income.
            elif not adjusted.needs_review and is_misfiled_income(
                adjusted.major, adjusted.minor, amount
            ):
                run.misfiled_income += 1
                adjusted = replace(
                    adjusted,
                    major=NEEDS_REVIEW[0],
                    minor=NEEDS_REVIEW[1],
                    flow_type=flow_type_for(NEEDS_REVIEW[0]),
                    confidence=0.0,
                    reason=(
                        f"Outgoing amount filed under "
                        f"{adjusted.major} / {adjusted.minor}, which is "
                        "income. The category it should have had cannot be "
                        "derived, so it needs review."
                    ),
                )
            # Recorded after the refund rule, which builds a fresh
            # Classification and would otherwise drop the provenance.
            match = paypal_matches.get(adjusted.transaction_id)
            if match is not None:
                adjusted = replace(
                    adjusted,
                    paypal_counterparty=match.counterparty,
                    paypal_settlement_code=match.settlement.code,
                )
            refiled.append(adjusted)
        run.classifications = refiled

        for classifier in (self._classifier, self._escalation_classifier):
            ledger = getattr(classifier, "ledger", None)
            if ledger is not None and ledger.calls:
                run.ledgers.append(ledger)

        logger.info("Classification run: %s", run.summary())
        return run

    def _paypal_fallback(self, transaction_id: str) -> Classification:
        major = settings.paypal_fallback_major
        minor = settings.paypal_fallback_minor
        major, minor = coerce_pair(major, minor)
        return Classification(
            transaction_id=transaction_id,
            major=major,
            minor=minor,
            flow_type=flow_type_for(major),
            # Deliberately zero: the category is a placeholder, not a finding.
            # The fallback runs before the threshold check on purpose, so this
            # does not send the transaction to review.
            confidence=0.0,
            reason="PayPal debit with no merchant in the booking text.",
            source=SOURCE_FALLBACK,
        )

    async def _ask_api(
        self,
        requests: list[ClassificationRequest],
        by_key: dict[str, list[Transaction]],
        merchants: dict[str, str],
        run: ClassificationRun,
    ) -> None:
        """Ask about each unknown merchant once, then fan the answer out."""
        classifier = self._get_classifier()
        results, failures = await classifier.classify(requests)
        run.usage = classifier.last_usage

        def group_of(key: str) -> list[Transaction]:
            return by_key.get(key, [])

        for result in results:
            group = group_of(result.id)
            if not group:
                continue
            merchant = merchants.get(str(group[0].id), "")
            decided = await self._accept(result, merchant, group[0].amount, run)
            run.api_calls += 1
            for transaction in group:
                run.classifications.append(
                    replace(decided, transaction_id=str(transaction.id))
                )

        for failure in failures:
            for key in failure.ids:
                for transaction in group_of(key):
                    run.failed += 1
                    run.classifications.append(
                        _review(
                            str(transaction.id),
                            f"Classification failed: {failure.error}",
                            merchants.get(str(transaction.id), ""),
                        )
                    )

    async def _escalate(
        self,
        requests: list[ClassificationRequest],
        by_key: dict[str, list[Transaction]],
        merchants: dict[str, str],
        run: ClassificationRun,
    ) -> None:
        """
        Re-ask the stronger model about everything that fell short.

        Only merchants that ended in review are sent, and only because they
        were unsure rather than invalid — a code that does not exist will not
        become one on a second reading, and a failed batch has already had its
        retries.
        """
        classifier = self._get_escalation_classifier()
        if classifier is None:
            return

        unsure = {
            classification.transaction_id
            for classification in run.classifications
            if classification.needs_review
            and classification.source == SOURCE_LLM
            and "below" in classification.reason
        }
        if not unsure:
            return

        by_transaction = {
            str(transaction.id): key
            for key, group in by_key.items()
            for transaction in group
        }
        keys = {by_transaction[tid] for tid in unsure if tid in by_transaction}
        retry = [request for request in requests if request.id in keys]
        if not retry:
            return

        logger.info("Escalating %s merchants to %s", len(retry), classifier.model)
        run.escalated += len(retry)

        results, _ = await classifier.classify(retry)
        improved: dict[str, Classification] = {}
        for result in results:
            group = by_key.get(result.id, [])
            if not group:
                continue
            merchant = merchants.get(str(group[0].id), "")
            decided = await self._accept(
                result, merchant, group[0].amount, run, classifier=classifier
            )
            if decided.needs_review:
                # No better than the first answer; leave the original in
                # place so the reason the user reads is the first one.
                continue
            run.escalation_resolved += 1
            for transaction in group:
                improved[str(transaction.id)] = replace(
                    decided, transaction_id=str(transaction.id)
                )

        if improved:
            run.classifications = [
                improved.get(classification.transaction_id, classification)
                for classification in run.classifications
            ]

    async def _accept(
        self,
        result: ClassificationResult,
        merchant: str,
        amount: float,
        run: ClassificationRun,
        classifier: ClaudeClassifier | None = None,
    ) -> Classification:
        """
        Validate one model result and decide whether to keep it.

        ``amount`` is the representative transaction's, and is only used to
        record which direction the merchant entry speaks for — every
        transaction in the group ran the same way, because the direction is
        part of the group key.
        """
        if not is_valid_code(result.code):
            run.invalid_codes += 1
            logger.warning("Discarded unknown category code %r", result.code)
            return _review(
                result.id,
                f"Model returned code {result.code!r}, which is not a category.",
                merchant,
            )

        major, minor = pair_for_code(result.code)

        if result.confidence < self.threshold:
            run.below_threshold += 1
            return _review(
                result.id,
                f"Confidence {result.confidence:.2f} below "
                f"{self.threshold:.2f}"
                + (f": {result.reason}" if result.reason else "."),
                merchant,
            )

        # Only a confident, valid answer is worth remembering — and a refund
        # is not one: it describes which way this transaction ran, not what
        # the merchant sells. Caching it would turn every later purchase at
        # that shop into a refund.
        if (major, minor) != REFUND_PAIR:
            await remember_merchant(
                merchant,
                major,
                minor,
                SOURCE_LLM,
                direction_of(amount),
                confidence=result.confidence,
                reason=result.reason or None,
                model_version=(classifier or self._get_classifier()).model,
            )

        return Classification(
            transaction_id=result.id,
            major=major,
            minor=minor,
            flow_type=flow_type_for(major),
            confidence=result.confidence,
            reason=result.reason,
            source=SOURCE_LLM,
            merchant=merchant,
        )


def refund_target(major: str, minor: str, amount: float) -> tuple[str, str] | None:
    """
    Where a transaction belongs when its category disagrees with its direction.

    A category answers "what was this for", and it is remembered per merchant.
    The direction is a property of the single transaction, and one merchant
    produces both: a purchase at Amazon and the refund for it carry the same
    merchant key and opposite meanings. Rather than hold a second cache entry
    for it — doubling the entries and halving the hit rate for a five percent
    case — this disagreement is resolved here.

    Returns the refund pair for an incoming amount filed as spending, and
    ``None`` when the category and the direction already agree. Income and
    transfers are never touched: a salary or a transfer between own accounts is
    positive by nature and means nothing of the sort.

    The reverse — an outgoing amount under an income category — cannot be
    repaired by a rule, because the category it should have had is not
    recoverable. :func:`is_misfiled_income` reports those instead, and
    :func:`entry_applies` stops the cache from creating new ones.
    """
    if amount <= 0:
        return None
    if (major, minor) == REFUND_PAIR or flow_type_for(major) != "expense":
        return None
    return REFUND_PAIR


def is_misfiled_income(major: str, minor: str, amount: float) -> bool:
    """
    An outgoing amount sitting under *any* income category.

    Income is money coming in. A negative amount under one of its minors is a
    contradiction whatever the minor is — a payment sent to the person who
    pays you maintenance is no more "other income" than a debit under refunds
    is a refund — and it is counted as income everywhere, because
    ``flow_type`` is derived from the major. No rule can repair it: the
    category it should have had is not recoverable from the one it has. These
    are reported so they can be classified again.
    """
    return amount < 0 and flow_type_for(major) == "income"


def apply_refund_rule(classification: Classification, amount: float) -> Classification:
    """Re-file a credit that was categorised as spending. See refund_target."""
    if classification.needs_review:
        return classification

    target = refund_target(classification.major, classification.minor, amount)
    if target is None:
        return classification

    major, minor = target
    return Classification(
        transaction_id=classification.transaction_id,
        major=major,
        minor=minor,
        flow_type=flow_type_for(major),
        confidence=classification.confidence,
        reason=(
            f"Incoming amount at a merchant otherwise filed under "
            f"{classification.major} / {classification.minor}, so a refund."
        ),
        source=classification.source,
        merchant=classification.merchant,
    )


async def apply(
    classifications: Iterable[Classification], transactions: Sequence[Transaction]
) -> int:
    """
    Write classifications onto their transactions and save them.

    Kept separate from classifying so a caller can look at a run before
    committing to it — the comparison mode and the evaluation both do.
    """
    by_id = {str(transaction.id): transaction for transaction in transactions}
    written = 0
    for classification in classifications:
        transaction = by_id.get(classification.transaction_id)
        if transaction is None:
            continue
        transaction.major_category = classification.major
        transaction.minor_category = classification.minor
        transaction.flow_type = classification.flow_type
        transaction.confidence = classification.confidence
        transaction.reason = classification.reason
        transaction.classification_source = classification.source
        if classification.paypal_counterparty:
            transaction.paypal_counterparty = classification.paypal_counterparty
            transaction.paypal_settlement_code = classification.paypal_settlement_code
        await transaction.save()
        written += 1
    return written


def report_lines(run: ClassificationRun, transaction_count: int) -> list[str]:
    """
    The per-import summary.

    Every number here answers a question the pipeline was built to answer: how
    many transactions collapsed onto how many merchants, how many of those the
    cache already knew, and what the rest cost.

    The last two lines are the category-against-direction rules: how often a
    merchant's remembered answer could not carry over to the other direction
    and had to be asked again, and how many outgoing amounts still ended up
    under an income category and were sent to review. Both should normally be
    small; a large one means a merchant is being read the wrong way round.
    """
    asked = run.api_calls
    # Counted from the finished classifications rather than from the number of
    # results inspected: escalation puts every unsure merchant through
    # validation a second time, so the per-call counters double up.
    in_review = sum(1 for c in run.classifications if c.needs_review)
    lines = [
        f"transactions         : {transaction_count}",
        f"  from merchant cache: {run.cache_hits}",
        f"  deduplicated       : {run.deduplicated}",
        f"  PayPal fallback    : {run.fallbacks}",
        f"merchants asked      : {asked}",
        f"  escalated          : {run.escalated} ({run.escalation_resolved} resolved)",
        f"in review            : {in_review}",
        f"  unsure judgements  : {run.below_threshold}",
        f"  invalid codes      : {run.invalid_codes}",
        f"  failed             : {run.failed}",
        f"re-filed as refunds  : {run.refunds}",
        f"asked for direction  : {run.direction_conflicts}",
        f"debits under income  : {run.misfiled_income}",
    ]

    answered = run.cache_hits + run.deduplicated + run.fallbacks
    total = answered + asked
    if total:
        lines.append(
            f"merchant cache saved : {answered / total * 100:.0f}% of possible calls"
        )

    for ledger in run.ledgers:
        models = ", ".join(sorted({call.model for call in ledger.calls}))
        lines.append(f"--- {models} ---")
        lines.extend(f"  {line}" for line in ledger.summary_lines())

    costs = [ledger.total_cost for ledger in run.ledgers]
    if costs and all(cost is not None for cost in costs):
        lines.append(f"estimated total cost : ${sum(costs):.4f}")
    return lines
