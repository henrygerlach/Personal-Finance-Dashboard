"""
Tests for the classification funnel.

These cover the decisions the service makes around the API rather than the API
itself: which transactions are asked about at all, what happens to a result
that does not validate, and who wins when the cache and the model disagree.
The classifier is a stub throughout — the point is the gatekeeping.
"""

import asyncio
from dataclasses import replace
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
from app.classification.service import ClassificationService, apply_refund_rule
from app.models import SOURCE_FALLBACK, SOURCE_LLM, SOURCE_USER


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


def make_transaction(tid, booking_text, amount=-10.0, process=None):
    return SimpleNamespace(
        id=tid,
        booking_text=booking_text,
        amount=amount,
        process=process,
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

    async def no_lookup(name):
        return None

    async def no_remember(name, major, minor, source, **kwargs):
        return None

    monkeypatch.setattr(service_module, "lookup_merchant", no_lookup)
    monkeypatch.setattr(service_module, "remember_merchant", no_remember)


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


def test_a_known_merchant_is_answered_from_the_cache(monkeypatch):
    saved = []

    async def lookup(name):
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


def test_use_cache_false_sends_everything_to_the_api(monkeypatch):
    """
    The evaluation needs this: scoring against a cache built from the same
    transactions would measure the cache, not the model.
    """

    async def lookup(name):
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

    async def remember(name, major, minor, source, **kwargs):
        remembered.append((name, major, minor, source))

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

    async def remember(name, major, minor, source, **kwargs):
        remembered.append((name, major, minor, source))

    monkeypatch.setattr(service_module, "remember_merchant", remember)

    stub = StubClassifier(results=[ClassificationResult("1", "3.1", 0.95)])
    run(
        ClassificationService(classifier=stub).classify(
            [make_transaction("1", " NEUER LADEN ")]
        )
    )
    assert remembered == [("NEUER LADEN", "Food & Drink", "Groceries", SOURCE_LLM)]


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
    transactions = [
        make_transaction("1", " A "),
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

    async def lookup(name):
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
