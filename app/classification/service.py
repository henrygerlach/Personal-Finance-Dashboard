"""
Classification as the application uses it.

The API is the last resort, not the first step. Every transaction goes through
the same funnel:

1. **The merchant cache.** A merchant somebody already categorised is answered
   from the database. A hand-corrected entry outranks anything the model would
   say, which is what makes a correction in the UI stick.
2. **The unresolvable-PayPal rule.** Comdirect truncates the merchant out of
   some PayPal lines. There is nothing in them to classify, so they are filed
   with the configured fallback rather than spending a call on a question with
   no answer. They are marked ``fallback`` and carry confidence 0, so they can
   be found again — the category is knowingly approximate.
3. **The API**, for what is left.

Every result the API returns is then checked twice: the pair must exist in the
hierarchy, and the confidence must clear the threshold. Anything that fails
either check becomes ``Uncategorized / Needs Review`` instead of a category
nobody chose.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from app.categories import NEEDS_REVIEW, coerce_pair, flow_type_for, is_valid_pair
from app.classification.client import (
    ClassificationRequest,
    ClassificationResult,
    ClaudeClassifier,
)
from app.config import get_logger, settings
from app.models import (
    SOURCE_FALLBACK,
    SOURCE_LLM,
    SOURCE_USER,
    MerchantCategory,
    Transaction,
)
from app.normalization import (
    is_unresolvable_paypal,
    merchant_key,
    normalize_merchant,
    shorten_merchant,
)

logger = get_logger(__name__)


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
    invalid_pairs: int = 0
    failed: int = 0
    usage: dict | None = None

    def summary(self) -> str:
        return (
            f"{len(self.classifications)} transactions: "
            f"{self.cache_hits} from cache, {self.api_calls} classified, "
            f"{self.fallbacks} fallback, {self.failed} failed; "
            f"{self.below_threshold} below threshold, "
            f"{self.invalid_pairs} invalid pairs -> review"
        )


async def lookup_merchant(name: str) -> MerchantCategory | None:
    """
    Find a cached category for a normalized merchant name.

    Falls back to the name with its trailing token dropped, because card
    payments append the town and direct debits do not. Without that retry the
    same shop would miss the entry its other spelling created.
    """
    if not name:
        return None

    entry = await MerchantCategory.find_one(
        MerchantCategory.merchant == merchant_key(name)
    )
    if entry is not None:
        return entry

    shorter = shorten_merchant(name)
    if shorter:
        return await MerchantCategory.find_one(
            MerchantCategory.merchant == merchant_key(shorter)
        )
    return None


async def remember_merchant(name: str, major: str, minor: str, source: str) -> None:
    """
    Write a merchant's category to the cache.

    A ``user`` entry is never overwritten by the classifier: once a category
    has been corrected by hand it is the answer for that merchant, and a later
    model run must not undo it.
    """
    if not name or not is_valid_pair(major, minor):
        return

    key = merchant_key(name)
    entry = await MerchantCategory.find_one(MerchantCategory.merchant == key)

    if entry is None:
        await MerchantCategory(
            merchant=key,
            display_name=name,
            major=major,
            minor=minor,
            source=source,
            hit_count=0,
            updated_at=datetime.utcnow(),
        ).insert()
        return

    if entry.source == SOURCE_USER and source != SOURCE_USER:
        return

    entry.display_name = name
    entry.major, entry.minor = major, minor
    entry.source = source
    entry.updated_at = datetime.utcnow()
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
    ) -> None:
        self._classifier = classifier
        self.threshold = (
            threshold
            if threshold is not None
            else settings.classification_confidence_threshold
        )

    def _get_classifier(self) -> ClaudeClassifier:
        if self._classifier is None:
            self._classifier = ClaudeClassifier()
        return self._classifier

    async def classify(
        self, transactions: Sequence[Transaction], use_cache: bool = True
    ) -> ClassificationRun:
        """
        Classify transactions, consulting the cache before the API.

        Args:
            transactions: The transactions to classify. Not saved here — the
                caller decides whether a result is written.
            use_cache: Set to False to send everything to the API. The
                evaluation needs this: answering from a cache built out of the
                same data it is scoring would measure nothing.
        """
        run = ClassificationRun(classifications=[])
        to_ask: list[ClassificationRequest] = []
        merchants: dict[str, str] = {}

        for transaction in transactions:
            transaction_id = str(transaction.id)
            name = normalize_merchant(transaction.booking_text, transaction.process)
            merchants[transaction_id] = name

            if is_unresolvable_paypal(transaction.booking_text):
                run.fallbacks += 1
                run.classifications.append(self._paypal_fallback(transaction_id))
                continue

            if use_cache:
                entry = await lookup_merchant(name)
                if entry is not None:
                    run.cache_hits += 1
                    entry.hit_count += 1
                    await entry.save()
                    run.classifications.append(
                        Classification(
                            transaction_id=transaction_id,
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

            to_ask.append(
                ClassificationRequest(
                    id=transaction_id,
                    text=name or transaction.booking_text[:120],
                    amount=transaction.amount,
                    process=transaction.process,
                )
            )

        if to_ask:
            await self._ask_api(to_ask, merchants, run)

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
        merchants: dict[str, str],
        run: ClassificationRun,
    ) -> None:
        classifier = self._get_classifier()
        results, failures = await classifier.classify(requests)
        run.usage = classifier.last_usage

        for result in results:
            run.api_calls += 1
            run.classifications.append(
                await self._accept(result, merchants.get(result.id, ""), run)
            )

        for failure in failures:
            for transaction_id in failure.ids:
                run.failed += 1
                run.classifications.append(
                    _review(
                        transaction_id,
                        f"Classification failed: {failure.error}",
                        merchants.get(transaction_id, ""),
                    )
                )

    async def _accept(
        self, result: ClassificationResult, merchant: str, run: ClassificationRun
    ) -> Classification:
        """Validate one model result and decide whether to keep it."""
        major, minor = coerce_pair(result.major, result.minor)
        if (major, minor) == NEEDS_REVIEW and (
            result.major,
            result.minor,
        ) != NEEDS_REVIEW:
            run.invalid_pairs += 1
            logger.warning("Discarded invalid pair %r / %r", result.major, result.minor)
            return _review(
                result.id,
                f"Model returned {result.major} / {result.minor}, "
                "which is not a category.",
                merchant,
            )

        if result.confidence < self.threshold:
            run.below_threshold += 1
            return _review(
                result.id,
                f"Confidence {result.confidence:.2f} below "
                f"{self.threshold:.2f}: {result.reason}",
                merchant,
            )

        # Only a confident, valid answer is worth remembering.
        await remember_merchant(merchant, major, minor, SOURCE_LLM)

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
        await transaction.save()
        written += 1
    return written
