"""
Tests for the classification funnel.

These cover the decisions the service makes around the API rather than the API
itself: which transactions are asked about at all, what happens to a result
that does not validate, and who wins when the cache and the model disagree.
The classifier is a stub throughout — the point is the gatekeeping.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.categories import NEEDS_REVIEW
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
    render_hierarchy,
)
from app.classification.service import ClassificationService
from app.models import SOURCE_FALLBACK, SOURCE_LLM, SOURCE_USER


class StubClassifier:
    """Returns prepared results without touching the network."""

    def __init__(self, results=(), failures=()):
        self.results = list(results)
        self.failures = list(failures)
        self.last_usage = {"cache_read_input_tokens": 0}
        self.seen: list[ClassificationRequest] = []

    async def classify(self, requests):
        self.seen.extend(requests)
        return self.results, self.failures


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

    async def no_remember(name, major, minor, source):
        return None

    monkeypatch.setattr(service_module, "lookup_merchant", no_lookup)
    monkeypatch.setattr(service_module, "remember_merchant", no_remember)


def run(coro):
    return asyncio.run(coro)


# --- The prompt --------------------------------------------------------------


def test_system_prompt_contains_the_whole_hierarchy():
    prompt = build_system_prompt()
    for line in render_hierarchy().splitlines():
        assert line.strip("- ").strip() in prompt


def test_system_prompt_is_stable_across_calls():
    """
    The system block carries the cache breakpoint. Anything that varies
    between requests — a timestamp, a count — would mean the cache never hits
    and nothing would report the loss except the bill.
    """
    assert build_system_prompt() == build_system_prompt()


def test_response_schema_enumerates_valid_names_only():
    schema = build_response_schema()
    item = schema["properties"]["results"]["items"]
    assert "Food & Drink" in item["properties"]["major"]["enum"]
    assert "Groceries" in item["properties"]["minor"]["enum"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"id", "major", "minor", "confidence", "reason"}


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

    stub = StubClassifier(
        results=[ClassificationResult("1", "Food & Drink", "Groceries", 0.9, "x")]
    )
    transaction = make_transaction("1", " EDEKA BOECKER ")
    result = run(
        ClassificationService(classifier=stub).classify([transaction], use_cache=False)
    )

    assert len(stub.seen) == 1
    assert result.cache_hits == 0


# --- Validating what came back ----------------------------------------------


def test_an_invalid_pair_is_discarded_rather_than_repaired():
    """
    "Rent" is a real minor, but not under Food & Drink. Repairing that would
    file the transaction under a category nobody chose, and it would be
    indistinguishable from a correct answer afterwards.
    """
    stub = StubClassifier(
        results=[ClassificationResult("1", "Food & Drink", "Rent", 0.99, "x")]
    )
    result = run(
        ClassificationService(classifier=stub).classify(
            [make_transaction("1", " SOMETHING ")]
        )
    )

    assert result.invalid_pairs == 1
    assert result.classifications[0].needs_review


def test_a_result_below_the_threshold_goes_to_review():
    stub = StubClassifier(
        results=[ClassificationResult("1", "Food & Drink", "Groceries", 0.55, "unsure")]
    )
    result = run(
        ClassificationService(classifier=stub, threshold=0.7).classify(
            [make_transaction("1", " SOMETHING ")]
        )
    )

    assert result.below_threshold == 1
    assert result.classifications[0].needs_review
    assert "0.55" in result.classifications[0].reason


def test_a_result_at_the_threshold_is_kept():
    stub = StubClassifier(
        results=[ClassificationResult("1", "Food & Drink", "Groceries", 0.7, "ok")]
    )
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

    async def remember(name, major, minor, source):
        remembered.append((name, major, minor, source))

    monkeypatch.setattr(service_module, "remember_merchant", remember)

    stub = StubClassifier(
        results=[ClassificationResult("1", "Food & Drink", "Groceries", 0.4, "x")]
    )
    run(
        ClassificationService(classifier=stub, threshold=0.7).classify(
            [make_transaction("1", " SOMETHING ")]
        )
    )
    assert remembered == []


def test_an_accepted_result_is_written_to_the_cache(monkeypatch):
    remembered = []

    async def remember(name, major, minor, source):
        remembered.append((name, major, minor, source))

    monkeypatch.setattr(service_module, "remember_merchant", remember)

    stub = StubClassifier(
        results=[ClassificationResult("1", "Food & Drink", "Groceries", 0.95, "x")]
    )
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
        results=[ClassificationResult("1", "Food & Drink", "Groceries", 0.9, "ok")],
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
        results=[ClassificationResult("1", "Food & Drink", "Groceries", 0.9, "ok")],
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
            ClassificationResult("1", "Income", "Salary & Wages", 0.95, "x"),
            ClassificationResult(
                "2", "Transfers & Savings", "Internal Transfer", 0.95, "y"
            ),
            ClassificationResult("3", "Food & Drink", "Groceries", 0.95, "z"),
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
