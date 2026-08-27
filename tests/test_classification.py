"""
Tests for the classification funnel.

These cover the decisions the service makes around the API rather than the API
itself: which transactions are asked about at all, what happens to a result
that does not validate, and who wins when the cache and the model disagree.
The classifier is a stub throughout — the point is the gatekeeping.
"""

import asyncio
from dataclasses import replace
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.categories import NEEDS_REVIEW, all_codes
from app.classification import service as service_module
from app.classification.client import (
    BatchFailure,
    ClassificationRequest,
    ClassificationResult,
    _batched,
)
from app.classification.prompt import (
    build_response_schema,
    build_system_prompt,
    render_examples,
    render_hierarchy,
)
from app.classification.service import (
    ClassificationService,
    apply_refund_rule,
    entry_applies,
    is_misfiled_income,
    refund_target,
    report_lines,
)
from app.models import SOURCE_FALLBACK, SOURCE_LLM, SOURCE_USER
from app.paypal import PayPalLedger, PayPalRow


class StubClassifier:
    """Returns prepared results without touching the network."""

    def __init__(self, results=(), failures=(), model="stub-model"):
        self.results = list(results)
        self.failures = list(failures)
        self.model = model
        self.last_usage = {"cache_read_input_tokens": 0}
        self.seen: list[ClassificationRequest] = []

    async def classify(self, requests):
        """
        Echo the ids back that were actually asked about.

        The service addresses merchants, not transactions, so the request ids
        are merchant keys. Rewriting them here keeps each test about the
        behaviour it names instead of about the key format.
        """
        self.seen.extend(requests)
        results = [
            replace(result, id=request.id)
            for result, request in zip(self.results, requests)
        ]
        failures = [
            BatchFailure(tuple(r.id for r in requests[len(results) :]), f.error)
            for f in self.failures
        ]
        return results, failures


def make_transaction(
    tid, booking_text, amount=-10.0, process=None, on=date(2024, 3, 6)
):
    return SimpleNamespace(
        id=tid,
        booking_text=booking_text,
        amount=amount,
        process=process,
        date=datetime(on.year, on.month, on.day),
        major_category=None,
        minor_category=None,
        flow_type=None,
        confidence=None,
        reason=None,
        classification_source=None,
    )


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """Neutralise the cache so each test opts into the behaviour it needs."""

    async def no_lookup(name, direction):
        return None

    async def no_remember(name, major, minor, source, direction, **kwargs):
        return None

    monkeypatch.setattr(service_module, "lookup_merchant", no_lookup)
    monkeypatch.setattr(service_module, "remember_merchant", no_remember)

    # A service that was not given a ledger would otherwise go looking for one
    # in a database that is not there. Tests that want PayPal recovery inject
    # a ledger of their own, which suppresses this.
    async def no_ledger(self):
        return self._paypal_ledger

    monkeypatch.setattr(
        service_module.ClassificationService, "_get_paypal_ledger", no_ledger
    )


def run(coro):
    return asyncio.run(coro)


# --- The prompt --------------------------------------------------------------


def test_system_prompt_contains_the_whole_hierarchy():
    prompt = build_system_prompt(0.7)
    for line in render_hierarchy().splitlines():
        assert line.strip("- ").strip() in prompt


def test_system_prompt_is_stable_across_calls():
    """
    The system block carries the cache breakpoint. Anything that varies
    between requests — a timestamp, a count — would mean the cache never hits
    and nothing would report the loss except the bill.
    """
    assert build_system_prompt(0.7) == build_system_prompt(0.7)


def test_response_schema_enumerates_valid_codes_only():
    """
    The enum is what makes an impossible category unrepresentable: it lists
    codes, and a code names a pair.
    """
    schema = build_response_schema()
    item = schema["properties"]["results"]["items"]
    assert set(item["properties"]["c"]["enum"]) == set(all_codes())
    assert item["additionalProperties"] is False
    # "r" is deliberately optional: it is only wanted below the threshold.
    assert set(item["required"]) == {"id", "c", "p"}
    assert "r" in item["properties"]


def test_the_prompt_asks_for_a_reason_only_below_the_threshold():
    prompt = build_system_prompt(0.7)
    assert "only" in prompt.lower() and "0.7" in prompt
    # An example above the threshold shows no reason, one below it does.
    rendered = render_examples(0.9)
    assert '"r":' in rendered
    assert '"r":' not in render_examples(0.0)


# --- Batching ----------------------------------------------------------------


def test_batching_covers_every_item_exactly_once():
    items = list(range(23))
    batches = list(_batched(items, 10))
    assert [len(b) for b in batches] == [10, 10, 3]
    assert [i for b in batches for i in b] == items


# --- The funnel --------------------------------------------------------------


def test_unresolvable_paypal_never_reaches_the_api():
    """
    Those lines contain no merchant at all. Asking about them would spend a
    call on a question with no answer in it.
    """
    stub = StubClassifier()
    transaction = make_transaction(
        "1",
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "1024177396132 PP.5189.PP . , Ihr Ei nkauf bei Ref. J82223",
    )
    result = run(ClassificationService(classifier=stub).classify([transaction]))

    assert stub.seen == []
    assert result.fallbacks == 1
    assert result.classifications[0].source == SOURCE_FALLBACK


def test_the_paypal_fallback_is_not_sent_to_review_by_the_threshold():
    """
    Its confidence is zero on purpose — the category is a placeholder, not a
    finding — so the fallback has to run before the threshold check or it
    would immediately undo itself.
    """
    stub = StubClassifier()
    transaction = make_transaction(
        "1",
        "PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "PP.5189.PP . , Ihr Ei nkauf bei Ref. AB1",
    )
    result = run(ClassificationService(classifier=stub).classify([transaction]))
    classification = result.classifications[0]

    assert classification.confidence == 0.0
    assert not classification.needs_review
    assert (classification.major, classification.minor) != NEEDS_REVIEW


TRUNCATED_PAYPAL = (
    "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
    "1024177396132 PP.5189.PP . , Ihr Ei nkauf bei Ref. J82223"
)


def paypal_ledger_naming(payee, amount=-10.0, on=date(2024, 3, 4)):
    """A ledger holding one purchase and the bank settlement that funded it."""
    return PayPalLedger(
        [
            PayPalRow(
                code="PAY1",
                date=on,
                kind="PayPal Express-Zahlung",
                status="Abgeschlossen",
                name=payee,
                currency="EUR",
                gross=amount,
                related_code="",
                item_title="",
            ),
            PayPalRow(
                code="FUND1",
                date=on,
                kind="Bankgutschrift auf PayPal-Konto",
                status="Ausstehend",
                name="",
                currency="EUR",
                gross=abs(amount),
                related_code="PAY1",
                item_title="",
            ),
        ]
    )


def test_a_recovered_paypal_payee_is_classified_as_an_ordinary_merchant():
    """
    The whole point of the recovery: once the payee is back, the line is not
    special any more and goes through the same funnel as everything else.
    """
    stub = StubClassifier(
        results=[ClassificationResult(id="x", code="7.1", confidence=0.9)]
    )
    service = ClassificationService(
        classifier=stub, paypal_ledger=paypal_ledger_naming("Spotify AB")
    )
    result = run(service.classify([make_transaction("1", TRUNCATED_PAYPAL)]))

    assert result.paypal_recovered == 1
    assert result.fallbacks == 0
    assert [request.text for request in stub.seen] == ["SPOTIFY AB"]
    assert result.classifications[0].source == SOURCE_LLM


def test_a_recovered_payee_is_recorded_on_the_classification():
    stub = StubClassifier(
        results=[ClassificationResult(id="x", code="7.1", confidence=0.9)]
    )
    service = ClassificationService(
        classifier=stub, paypal_ledger=paypal_ledger_naming("Spotify AB")
    )
    result = run(service.classify([make_transaction("1", TRUNCATED_PAYPAL)]))
    classification = result.classifications[0]

    assert classification.paypal_counterparty == "Spotify AB"
    assert classification.paypal_settlement_code == "FUND1"


def test_the_fallback_still_applies_when_the_ledger_cannot_name_the_payee():
    """No export, or no unambiguous match, and the old behaviour is intact."""
    stub = StubClassifier()
    service = ClassificationService(
        classifier=stub,
        # The right payee, but for an amount this transaction does not have.
        paypal_ledger=paypal_ledger_naming("Spotify AB", amount=-99.0),
    )
    result = run(service.classify([make_transaction("1", TRUNCATED_PAYPAL)]))

    assert stub.seen == []
    assert result.paypal_recovered == 0
    assert result.fallbacks == 1
    assert result.classifications[0].source == SOURCE_FALLBACK


def test_a_recovered_payee_merges_with_the_same_merchant_named_in_the_clear():
    """
    One request, not two: the recovered name is folded in before merchants are
    grouped, so a truncated Spotify line and a legible one are one merchant.
    """
    stub = StubClassifier(
        results=[ClassificationResult(id="x", code="7.1", confidence=0.9)]
    )
    service = ClassificationService(
        classifier=stub, paypal_ledger=paypal_ledger_naming("Spotify AB")
    )
    result = run(
        service.classify(
            [
                make_transaction("1", TRUNCATED_PAYPAL),
                make_transaction("2", "Auftraggeber: Spotify AB"),
            ]
        )
    )

    assert len(stub.seen) == 1
    assert result.deduplicated == 1
    assert len(result.classifications) == 2


def test_a_paypal_line_that_names_its_merchant_is_left_alone():
    """
    The export is consulted only where the bank has nothing to say. A legible
    PayPal line is already an ordinary merchant line.
    """
    stub = StubClassifier(
        results=[ClassificationResult(id="x", code="7.1", confidence=0.9)]
    )
    service = ClassificationService(
        classifier=stub, paypal_ledger=paypal_ledger_naming("Somebody Else")
    )
    legible = (
        "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. Buchungstext: "
        "PP.5189.PP . SPOTIFY, Ihr Einkauf bei SPOTIFY"
    )
    result = run(service.classify([make_transaction("1", legible)]))

    assert result.paypal_recovered == 0
    assert [request.text for request in stub.seen] == ["SPOTIFY"]


def test_a_known_merchant_is_answered_from_the_cache(monkeypatch):
    saved = []

    async def lookup(name, direction):
        entry = SimpleNamespace(
            major="Food & Drink",
            minor="Groceries",
            source=SOURCE_USER,
            hit_count=3,
        )

        async def save():
            saved.append(entry)

        entry.save = save
        return entry

    monkeypatch.setattr(service_module, "lookup_merchant", lookup)

    stub = StubClassifier()
    transaction = make_transaction("1", " EDEKA BOECKER HAMBURG 000 ")
    result = run(ClassificationService(classifier=stub).classify([transaction]))

    assert stub.seen == []
    assert result.cache_hits == 1
    assert result.classifications[0].minor == "Groceries"
    assert saved and saved[0].hit_count == 4


def test_a_debit_at_an_income_merchant_is_asked_about_instead_of_cached(monkeypatch):
    """
    The live bug, from the top. One person paid maintenance *and* was paid by
    PayPal. A single cache entry saying "Other Income" answered both
    directions, so seven outgoing payments were filed as income. The entry
    learned from the incoming side must not answer the outgoing one — the
    group is asked about in its own right, and the answer is then remembered
    for its own direction.
    """
    asked = []

    async def lookup(name, direction):
        asked.append(direction)
        entry = SimpleNamespace(
            major="Income",
            minor="Other Income",
            source=SOURCE_USER,
            hit_count=68,
        )

        async def save():
            raise AssertionError("a rejected entry must not be touched")

        entry.save = save
        return entry

    monkeypatch.setattr(service_module, "lookup_merchant", lookup)

    stub = StubClassifier(results=[ClassificationResult("1", "13.8", 0.9)])
    transaction = make_transaction("1", " MAREN GERLACH ", amount=-52.50)
    result = run(ClassificationService(classifier=stub).classify([transaction]))

    assert asked == ["out"]
    assert result.cache_hits == 0
    assert result.direction_conflicts == 1
    assert len(stub.seen) == 1
    assert result.classifications[0].minor == "Person to Person"


def test_the_same_merchant_keeps_its_answer_for_the_other_direction(monkeypatch):
    """The incoming side of that merchant is untouched and still cached."""

    async def lookup(name, direction):
        assert direction == "in"
        entry = SimpleNamespace(
            major="Income",
            minor="Other Income",
            source=SOURCE_USER,
            hit_count=68,
        )

        async def save():
            return None

        entry.save = save
        return entry

    monkeypatch.setattr(service_module, "lookup_merchant", lookup)

    stub = StubClassifier()
    transaction = make_transaction("1", " MAREN GERLACH ", amount=500.0)
    result = run(ClassificationService(classifier=stub).classify([transaction]))

    assert stub.seen == []
    assert result.cache_hits == 1
    assert result.classifications[0].minor == "Other Income"


def test_use_cache_false_sends_everything_to_the_api(monkeypatch):
    """
    The evaluation needs this: scoring against a cache built from the same
    transactions would measure the cache, not the model.
    """

    async def lookup(name, direction):
        raise AssertionError("cache must not be consulted")

    monkeypatch.setattr(service_module, "lookup_merchant", lookup)

    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.9)])
    transaction = make_transaction("1", " EDEKA BOECKER ")
    result = run(
        ClassificationService(classifier=stub).classify([transaction], use_cache=False)
    )

    assert len(stub.seen) == 1
    assert result.cache_hits == 0


# --- Validating what came back ----------------------------------------------


def test_an_unknown_code_is_discarded_rather_than_repaired():
    """
    A code outside the hierarchy means the model produced something that is
    not a category. Repairing it would file the transaction under a category
    nobody chose, indistinguishable from a correct answer afterwards.
    """
    stub = StubClassifier(results=[ClassificationResult("1", "3.99", 0.99)])
    result = run(
        ClassificationService(classifier=stub).classify(
            [make_transaction("1", " SOMETHING ")]
        )
    )

    assert result.invalid_codes == 1
    assert result.classifications[0].needs_review


def test_a_result_below_the_threshold_goes_to_review():
    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.55, "unsure")])
    result = run(
        ClassificationService(classifier=stub, threshold=0.7).classify(
            [make_transaction("1", " SOMETHING ")]
        )
    )

    assert result.below_threshold == 1
    assert result.classifications[0].needs_review
    assert "0.55" in result.classifications[0].reason


def test_a_result_at_the_threshold_is_kept():
    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.7)])
    result = run(
        ClassificationService(classifier=stub, threshold=0.7).classify(
            [make_transaction("1", " SOMETHING ")]
        )
    )

    assert result.below_threshold == 0
    assert not result.classifications[0].needs_review


def test_a_low_confidence_result_is_not_written_to_the_cache(monkeypatch):
    """A guess must not become the remembered answer for a merchant."""
    remembered = []

    async def remember(name, major, minor, source, direction, **kwargs):
        remembered.append((name, major, minor, source, direction))

    monkeypatch.setattr(service_module, "remember_merchant", remember)

    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.4, "unsure")])
    run(
        ClassificationService(classifier=stub, threshold=0.7).classify(
            [make_transaction("1", " SOMETHING ")]
        )
    )
    assert remembered == []


def test_an_accepted_result_is_written_to_the_cache(monkeypatch):
    remembered = []

    async def remember(name, major, minor, source, direction, **kwargs):
        remembered.append((name, major, minor, source, direction))

    monkeypatch.setattr(service_module, "remember_merchant", remember)

    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.95)])
    run(
        ClassificationService(classifier=stub).classify(
            [make_transaction("1", " NEUER LADEN ")]
        )
    )
    assert remembered == [
        ("NEUER LADEN", "Food & Drink", "Groceries", SOURCE_LLM, "out")
    ]


# --- Failure isolation -------------------------------------------------------


def test_a_failed_batch_becomes_review_rather_than_an_exception():
    """One bad batch must not take the rest of an import down with it."""
    stub = StubClassifier(
        results=[ClassificationResult("1", "3.1", 0.9)],
        failures=[BatchFailure(("2",), "500: upstream")],
    )
    result = run(
        ClassificationService(classifier=stub).classify(
            [make_transaction("1", " A SHOP "), make_transaction("2", " B SHOP ")]
        )
    )

    assert result.failed == 1
    by_id = {c.transaction_id: c for c in result.classifications}
    assert not by_id["1"].needs_review
    assert by_id["2"].needs_review
    assert "upstream" in by_id["2"].reason


def test_every_transaction_gets_exactly_one_classification():
    """
    Nothing may be dropped silently: a transaction with no classification
    would keep whatever category it had and look like a success.
    """
    stub = StubClassifier(
        results=[ClassificationResult("1", "3.1", 0.9)],
        failures=[BatchFailure(("2", "3"), "boom")],
    )
    transactions = [
        make_transaction("1", " A "),
        make_transaction("2", " B "),
        make_transaction("3", " C "),
    ]
    result = run(ClassificationService(classifier=stub).classify(transactions))

    assert len(result.classifications) == 3
    assert {c.transaction_id for c in result.classifications} == {"1", "2", "3"}


def test_flow_type_is_derived_not_taken_from_the_model():
    stub = StubClassifier(
        results=[
            ClassificationResult("1", "1.1", 0.95),
            ClassificationResult("2", "13.1", 0.95),
            ClassificationResult("3", "3.1", 0.95),
        ]
    )
    # Each amount runs the way its category implies: a salary is a credit, a
    # transfer is either, groceries are a debit. A category that contradicts
    # the direction is a different rule's business, and it would send the
    # transaction to review before this one could be observed.
    transactions = [
        make_transaction("1", " A ", amount=2400.0),
        make_transaction("2", " B "),
        make_transaction("3", " C "),
    ]
    result = run(ClassificationService(classifier=stub).classify(transactions))
    flows = {c.transaction_id: c.flow_type for c in result.classifications}

    assert flows == {"1": "income", "2": "transfer", "3": "expense"}


# --- The refund rule ---------------------------------------------------------


def _classification(major, minor, transaction_id="1", confidence=0.9):
    from app.categories import flow_type_for
    from app.classification.service import Classification

    return Classification(
        transaction_id=transaction_id,
        major=major,
        minor=minor,
        flow_type=flow_type_for(major),
        confidence=confidence,
        reason="",
        source=SOURCE_LLM,
        merchant="A MERCHANT",
    )


def test_an_incoming_amount_at_an_expense_merchant_becomes_a_refund():
    """
    The cache answers per merchant, and one merchant produces both directions:
    a purchase and the refund for it share a key. Keying the cache on the
    amount too would double the entries for a five percent case.
    """
    refiled = apply_refund_rule(_classification("Shopping", "Electronics & Tech"), 4.49)
    assert (refiled.major, refiled.minor) == ("Income", "Refunds & Reimbursements")
    assert refiled.flow_type == "income"


def test_an_outgoing_amount_is_left_alone():
    original = _classification("Shopping", "Electronics & Tech")
    assert apply_refund_rule(original, -4.49) is original


@pytest.mark.parametrize(
    "major,minor",
    [
        ("Income", "Salary & Wages"),
        ("Transfers & Savings", "Internal Transfer"),
        ("Transfers & Savings", "Cash Deposit"),
    ],
)
def test_income_and_transfers_are_never_re_filed(major, minor):
    """
    A salary or a transfer between the owner's own accounts is positive by
    nature; calling either one a refund would be wrong.
    """
    original = _classification(major, minor)
    assert apply_refund_rule(original, 1500.0) is original


def test_a_transaction_in_review_is_not_re_filed():
    """A refund is still a category, and review means no category was found."""
    original = _classification(*NEEDS_REVIEW)
    assert apply_refund_rule(original, 10.0) is original


def test_the_refund_rule_runs_on_cache_hits_too(monkeypatch):
    """
    It has to cover every path — a merchant answered from the cache is
    exactly where the sign problem shows up, because the cache has one
    category for both directions.
    """

    async def lookup(name, direction):
        entry = SimpleNamespace(
            major="Shopping",
            minor="General Merchandise",
            source=SOURCE_USER,
            hit_count=0,
            last_used=None,
        )

        async def save():
            return None

        entry.save = save
        return entry

    monkeypatch.setattr(service_module, "lookup_merchant", lookup)

    stub = StubClassifier()
    transaction = make_transaction("1", " AMAZON ", amount=4.49)
    result = run(ClassificationService(classifier=stub).classify([transaction]))

    assert result.cache_hits == 1
    assert result.refunds == 1
    assert result.classifications[0].minor == "Refunds & Reimbursements"


# --- Deduplication -----------------------------------------------------------


def test_one_merchant_seen_many_times_is_asked_about_once(monkeypatch):
    """
    The point of the whole pipeline: a monthly import of 150 transactions must
    not become 150 API calls.
    """
    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.95)])
    transactions = [
        make_transaction("1", " EDEKA BOECKER "),
        make_transaction("2", " EDEKA BOECKER HAMBURG 000 "),
        make_transaction("3", " EDEKA BOECKER "),
    ]
    result = run(
        ClassificationService(classifier=stub).classify(transactions, use_cache=False)
    )

    assert len(stub.seen) == 1, "the same merchant was sent more than once"
    assert len(result.classifications) == 3
    assert {c.minor for c in result.classifications} == {"Groceries"}
    assert result.deduplicated == 2


# --- Escalation --------------------------------------------------------------


def test_an_injected_classifier_never_grows_a_real_one_behind_it(monkeypatch):
    """
    A caller that supplies its own classifier is controlling API access
    deliberately. Building a second, real client for the escalation pass would
    put live calls into the test suite — which is exactly how this was found.
    """

    def explode(*args, **kwargs):
        raise AssertionError("a real classifier was constructed")

    monkeypatch.setattr(service_module, "ClaudeClassifier", explode)

    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.2, "unsure")])
    service = ClassificationService(classifier=stub)
    result = run(service.classify([make_transaction("1", " A SHOP ")]))

    assert service._get_escalation_classifier() is None
    assert result.escalated == 0
    assert result.classifications[0].needs_review


def test_escalation_re_asks_only_what_fell_short():
    """
    A code that does not exist will not start existing on a second reading,
    and a failed batch has already had its retries. Only genuine uncertainty
    is worth a stronger model.
    """
    weak = StubClassifier(
        results=[
            ClassificationResult("1", "3.1", 0.4, "unsure"),
            ClassificationResult("2", "3.1", 0.95),
            ClassificationResult("3", "99.9", 0.99),
        ]
    )
    strong = StubClassifier(
        results=[ClassificationResult("1", "3.1", 0.93)], model="strong-model"
    )
    transactions = [
        make_transaction("1", " UNSURE SHOP "),
        make_transaction("2", " CLEAR SHOP "),
        make_transaction("3", " BOGUS SHOP "),
    ]
    result = run(
        ClassificationService(classifier=weak, escalation_classifier=strong).classify(
            transactions, use_cache=False
        )
    )

    assert result.escalated == 1, "only the unsure merchant should be re-asked"
    assert [r.text for r in strong.seen] == ["UNSURE SHOP"]
    assert result.escalation_resolved == 1

    by_id = {c.transaction_id: c for c in result.classifications}
    assert by_id["1"].minor == "Groceries"
    assert by_id["1"].confidence == 0.93
    assert by_id["3"].needs_review, "an invalid code stays in review"


def test_escalation_that_stays_unsure_leaves_the_first_answer_alone():
    """
    Otherwise the reason a person reads in the review queue would be the
    second model's, which says nothing the first one did not.
    """
    weak = StubClassifier(
        results=[ClassificationResult("1", "3.1", 0.4, "first doubt")]
    )
    strong = StubClassifier(
        results=[ClassificationResult("1", "3.1", 0.5, "still unsure")],
        model="strong-model",
    )
    result = run(
        ClassificationService(classifier=weak, escalation_classifier=strong).classify(
            [make_transaction("1", " A SHOP ")], use_cache=False
        )
    )

    assert result.escalated == 1
    assert result.escalation_resolved == 0
    assert "first doubt" in result.classifications[0].reason


def test_escalation_can_be_switched_off():
    weak = StubClassifier(results=[ClassificationResult("1", "3.1", 0.2, "x")])
    strong = StubClassifier(model="strong-model")
    result = run(
        ClassificationService(
            classifier=weak, escalation_classifier=strong, escalate=False
        ).classify([make_transaction("1", " A SHOP ")], use_cache=False)
    )
    assert result.escalated == 0
    assert strong.seen == []


def test_an_escalated_result_is_cached_under_the_stronger_model():
    """model_version has to name the model that actually decided."""
    remembered = []

    async def remember(name, major, minor, source, direction, **kwargs):
        remembered.append((name, kwargs.get("model_version")))

    weak = StubClassifier(results=[ClassificationResult("1", "3.1", 0.4, "x")])
    strong = StubClassifier(
        results=[ClassificationResult("1", "3.1", 0.95)], model="strong-model"
    )
    service = ClassificationService(classifier=weak, escalation_classifier=strong)
    service_module.remember_merchant = remember
    try:
        run(service.classify([make_transaction("1", " A SHOP ")], use_cache=False))
    finally:
        pass

    assert remembered == [("A SHOP", "strong-model")]


# --- The import report -------------------------------------------------------


def test_the_report_counts_review_from_the_result_not_from_the_calls():
    """
    Escalation validates every unsure merchant a second time, so the per-call
    counters double up. The line a person reads has to be the finished state.
    """
    weak = StubClassifier(results=[ClassificationResult("1", "3.1", 0.4, "x")])
    strong = StubClassifier(
        results=[ClassificationResult("1", "3.1", 0.5, "still unsure")],
        model="strong-model",
    )
    result = run(
        ClassificationService(classifier=weak, escalation_classifier=strong).classify(
            [make_transaction("1", " A SHOP ")], use_cache=False
        )
    )

    assert result.below_threshold == 2, "both passes judged it unsure"
    report = "\n".join(report_lines(result, 1))
    assert "in review            : 1" in report


def test_the_report_survives_a_run_that_made_no_api_calls():
    stub = StubClassifier()
    transaction = make_transaction(
        "1",
        "PayPal (Europe) Buchungstext: PP.5189.PP . , Ihr Ei nkauf bei Ref. A1",
    )
    result = run(ClassificationService(classifier=stub).classify([transaction]))
    report = "\n".join(report_lines(result, 1))
    assert "merchants asked      : 0" in report
    assert "PayPal fallback    : 1" in report


# --- Category against direction ----------------------------------------------


def test_a_credit_under_an_expense_category_is_a_refund():
    assert refund_target("Shopping", "Clothing & Shoes", 133.65) == (
        "Income",
        "Refunds & Reimbursements",
    )
    assert refund_target("Travel", "Accommodation", 55.36) == (
        "Income",
        "Refunds & Reimbursements",
    )


def test_a_debit_under_an_expense_category_is_left_alone():
    assert refund_target("Shopping", "Clothing & Shoes", -133.65) is None


@pytest.mark.parametrize(
    "major,minor",
    [
        ("Income", "Salary & Wages"),
        ("Income", "Refunds & Reimbursements"),
        ("Transfers & Savings", "Internal Transfer"),
        ("Transfers & Savings", "Cash Deposit"),
    ],
)
def test_income_and_transfers_are_never_re_filed_as_refunds(major, minor):
    """A salary and a deposit are positive by nature and mean nothing of the sort."""
    assert refund_target(major, minor, 1500.0) is None


def test_a_debit_under_any_income_category_is_reported_as_unrepairable():
    """
    The mirror case. It cannot be fixed by a rule — the category it should
    have had is not recoverable — so it has to be found and re-classified
    rather than silently rewritten.

    It is not specific to refunds. Seven transactions in the live data sat
    under Income / Other Income at -168.38 EUR, because the person who pays
    maintenance was also paid *by* PayPal, and one cache entry answered for
    both directions. Every income minor has to be caught, not just the one
    whose name suggests a direction.
    """
    assert is_misfiled_income("Income", "Refunds & Reimbursements", -4.49)
    assert is_misfiled_income("Income", "Other Income", -52.50)
    assert is_misfiled_income("Income", "Salary & Wages", -1500.0)
    assert not is_misfiled_income("Income", "Other Income", 52.50)
    assert not is_misfiled_income("Shopping", "Clothing & Shoes", -4.49)
    assert not is_misfiled_income("Transfers & Savings", "Person to Person", -4.49)


def test_every_income_minor_is_covered():
    """
    An exhaustive check, because the bug this exists to catch was a single
    minor being special-cased.
    """
    from app.categories import CATEGORY_HIERARCHY

    for minor in CATEGORY_HIERARCHY["Income"]:
        assert is_misfiled_income("Income", minor, -10.0)
        assert not is_misfiled_income("Income", minor, 10.0)


def test_a_debit_at_an_income_merchant_is_not_answered_from_the_cache():
    """
    ``entry_applies`` is what stops the live bug from happening again. A
    remembered income category may answer a credit, and a remembered expense
    category may answer a credit too — that is a refund, and the refund rule
    rewrites it. Only a debit at an income merchant has no reading at all.
    """
    assert entry_applies("Income", 52.50)
    assert not entry_applies("Income", -52.50)
    assert entry_applies("Shopping", -52.50)
    # A credit at an expense merchant: still answered, then re-filed as a
    # refund. Keeping it in the cache is what avoids a second entry per shop.
    assert entry_applies("Shopping", 52.50)
    # A transfer means the same thing whichever way it ran.
    assert entry_applies("Transfers & Savings", 52.50)
    assert entry_applies("Transfers & Savings", -52.50)


def test_a_debit_that_still_carries_an_income_category_goes_to_review():
    """
    The last line of defence, for an answer the cache never saw: whatever
    produced it, an outgoing amount must not be counted as income.
    """
    stub = StubClassifier(results=[ClassificationResult("1", "1.6", 0.95)])
    transaction = make_transaction("1", " MAREN GERLACH ", amount=-52.50)
    result = run(ClassificationService(classifier=stub).classify([transaction]))

    assert result.misfiled_income == 1
    decided = result.classifications[0]
    assert (decided.major, decided.minor) == NEEDS_REVIEW
    assert "Other Income" in decided.reason


def test_the_rule_holds_for_every_category_in_the_hierarchy():
    """
    An exhaustive check rather than a sample: a credit must never stay under
    an expense category, and nothing else may be touched.
    """
    from app.categories import flow_type_for, iter_pairs

    for major, minor in iter_pairs():
        credit = refund_target(major, minor, 10.0)
        debit = refund_target(major, minor, -10.0)
        assert debit is None
        if flow_type_for(major) == "expense":
            assert credit == ("Income", "Refunds & Reimbursements")
        else:
            assert credit is None
