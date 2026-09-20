"""
Tests for the merchant cache and for writing a run back onto transactions.

The cache is the first step of the funnel and the reason most transactions
never reach the API, so two rules decide whether it can be trusted. A
hand-made correction outranks the model permanently — otherwise the next run
quietly undoes the work the review queue exists to collect. And an entry
belongs to a merchant *and a direction*, because a purchase at Amazon and the
refund for it are the same merchant meaning two different things; one entry
answering for both filed outgoing payments as refunds.

``tests/test_classification.py`` covers the funnel's decisions with the cache
stubbed out. What is exercised here is the cache itself, against the in-memory
documents in :mod:`tests.fixtures.odm`.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.classification import service as service_module
from app.classification.service import apply, lookup_merchant, remember_merchant
from app.models import DIRECTION_IN, DIRECTION_OUT, SOURCE_LLM, SOURCE_USER
from tests.fixtures.odm import document


def run(coro):
    return asyncio.run(coro)


USER_ID = "u" * 24


@pytest.fixture
def cache(monkeypatch):
    cache_model = document("MerchantCategory", "merchant_key", "direction", "user_id")
    monkeypatch.setattr(service_module, "MerchantCategory", cache_model)
    return cache_model


def remember(
    name,
    major="Food & Drink",
    minor="Groceries",
    source=SOURCE_LLM,
    direction=DIRECTION_OUT,
    **extra,
):
    return run(
        remember_merchant(USER_ID, name, major, minor, source, direction, **extra)
    )


# --- Writing ------------------------------------------------------------------


def test_a_merchant_is_remembered_for_the_direction_it_was_learned_from(cache):
    """
    The direction is part of the entry's identity, so a correction to an
    outgoing payment leaves what was learned from the incoming ones alone.
    """
    remember("EDEKA", direction=DIRECTION_OUT)

    assert len(cache.rows) == 1
    assert cache.rows[0].direction == DIRECTION_OUT


def test_the_same_merchant_can_hold_one_entry_per_direction(cache):
    """
    A merchant that genuinely runs both ways needs two answers, and writing
    the second must not overwrite the first.
    """
    remember("AMAZON", "Shopping", "General Merchandise", direction=DIRECTION_OUT)
    remember("AMAZON", "Income", "Refunds & Reimbursements", direction=DIRECTION_IN)

    assert len(cache.rows) == 2


def test_writing_the_same_merchant_again_updates_rather_than_duplicates(cache):
    """
    Two entries for one merchant and one direction would make which answer
    wins depend on insertion order.
    """
    remember("EDEKA", "Food & Drink", "Groceries")
    remember("EDEKA", "Food & Drink", "Cafés & Snacks")

    assert len(cache.rows) == 1
    assert cache.rows[0].minor == "Cafés & Snacks"


def test_a_hand_made_entry_is_never_overwritten_by_the_model(cache):
    """
    This is what makes a correction in the review queue stick. Without it the
    next classification run silently undoes the work the queue collects.
    """
    remember("NYA HERING", "Housing", "Maintenance & Services", source=SOURCE_USER)

    remember("NYA HERING", "Shopping", "General Merchandise", source=SOURCE_LLM)

    assert cache.rows[0].major == "Housing"
    assert cache.rows[0].source == SOURCE_USER


def test_a_correction_can_replace_an_earlier_correction(cache):
    """The user is allowed to change their mind; only the model is not."""
    remember("NYA HERING", "Housing", "Rent", source=SOURCE_USER)

    remember("NYA HERING", "Housing", "Maintenance & Services", source=SOURCE_USER)

    assert cache.rows[0].minor == "Maintenance & Services"


def test_a_correction_replaces_what_the_model_had_said(cache):
    remember("EDEKA", "Shopping", "General Merchandise", source=SOURCE_LLM)

    remember("EDEKA", "Food & Drink", "Groceries", source=SOURCE_USER)

    assert cache.rows[0].major == "Food & Drink"
    assert cache.rows[0].source == SOURCE_USER


def test_a_pair_that_is_not_in_the_hierarchy_is_not_remembered(cache):
    """
    An invalid pair reaching the cache would be answered confidently forever,
    without ever going through the validation that rejected it once.
    """
    remember("EDEKA", "Invented", "Nonsense")

    assert cache.rows == []


def test_a_merchant_with_no_name_is_not_remembered(cache):
    """
    A truncated PayPal line normalises to nothing. An entry under the empty
    key would answer for every other one of them.
    """
    remember("")

    assert cache.rows == []


def test_the_model_version_is_recorded_so_old_entries_can_be_found(cache):
    """
    Entries written by an older model or prompt have to be re-runnable
    without disturbing the hand-made ones.
    """
    remember("EDEKA", model_version="claude-haiku-4-5")

    assert cache.rows[0].model_version == "claude-haiku-4-5"


# --- Reading ------------------------------------------------------------------


def test_a_remembered_merchant_is_found_again(cache):
    remember("EDEKA")

    entry = run(lookup_merchant(USER_ID, "EDEKA", DIRECTION_OUT))

    assert entry is not None and entry.minor == "Groceries"


def test_a_merchant_nobody_has_seen_is_a_miss(cache):
    assert run(lookup_merchant(USER_ID, "NEVER SEEN", DIRECTION_OUT)) is None


def test_a_lookup_with_no_name_is_a_miss_rather_than_a_query(cache):
    """Nothing can be said about a booking line holding no merchant."""
    assert run(lookup_merchant(USER_ID, "", DIRECTION_OUT)) is None


def test_the_asked_for_direction_wins_over_the_other_one(cache):
    """
    Both exist for a two-way merchant, and answering with the wrong one files
    a purchase as a refund.
    """
    remember("AMAZON", "Shopping", "General Merchandise", direction=DIRECTION_OUT)
    remember("AMAZON", "Income", "Refunds & Reimbursements", direction=DIRECTION_IN)

    assert run(lookup_merchant(USER_ID, "AMAZON", DIRECTION_OUT)).major == "Shopping"
    assert run(lookup_merchant(USER_ID, "AMAZON", DIRECTION_IN)).major == "Income"


def test_an_entry_learned_the_other_way_round_still_answers(cache):
    """
    Most merchants only ever run one way, so the first refund from one would
    otherwise miss its own entry. Whether the answer may be used is the
    caller's decision, not the lookup's.
    """
    remember("EDEKA", direction=DIRECTION_OUT)

    assert run(lookup_merchant(USER_ID, "EDEKA", DIRECTION_IN)) is not None


def test_a_merchant_found_under_a_shorter_spelling_is_still_found(cache):
    """
    Comdirect appends a branch or a town to some merchants, so the same shop
    arrives under two spellings. Looking the shortened key up as well is what
    keeps those one merchant rather than two cache misses and two API calls.
    """
    remember("REWE MARKT GMBH")

    assert (
        run(lookup_merchant(USER_ID, "REWE MARKT GMBH BERLIN", DIRECTION_OUT))
        is not None
    )


# --- Writing a run back onto transactions -------------------------------------


def classification(transaction_id="t1", **extra):
    defaults = dict(
        transaction_id=transaction_id,
        major="Food & Drink",
        minor="Groceries",
        flow_type="expense",
        confidence=0.93,
        reason="",
        source=SOURCE_LLM,
        merchant="EDEKA",
        paypal_counterparty=None,
        paypal_settlement_code=None,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


class FakeTransaction(SimpleNamespace):
    """A transaction that records having been saved."""

    async def save(self):
        self.saved = True


def transaction(tid="t1"):
    return FakeTransaction(
        id=tid,
        major_category="Uncategorized",
        minor_category="Needs Review",
        flow_type=None,
        confidence=None,
        reason=None,
        classification_source=None,
        paypal_counterparty=None,
        paypal_settlement_code=None,
        saved=False,
    )


def test_a_classification_is_written_onto_its_transaction():
    """
    Applying is separate from classifying so a run can be inspected first —
    the comparison mode and the evaluation both do exactly that.
    """
    row = transaction()

    written = run(apply([classification()], [row]))

    assert written == 1
    assert (row.major_category, row.minor_category) == ("Food & Drink", "Groceries")
    assert row.flow_type == "expense"
    assert row.classification_source == SOURCE_LLM
    assert row.saved is True


def test_a_classification_for_a_transaction_that_is_not_here_is_skipped():
    """
    The caller decides which transactions to write, and a run can name one it
    was not given. Skipping is right; raising would lose the rest of the run.
    """
    row = transaction("t1")

    assert run(apply([classification("somebody-else")], [row])) == 0
    assert row.saved is False


def test_a_recovered_paypal_payee_is_written_with_its_source():
    """
    The settlement code is what lets a recovered merchant be audited back to
    the export it came from.
    """
    row = transaction()

    run(
        apply(
            [
                classification(
                    paypal_counterparty="Spotify AB", paypal_settlement_code="SET1"
                )
            ],
            [row],
        )
    )

    assert row.paypal_counterparty == "Spotify AB"
    assert row.paypal_settlement_code == "SET1"


def test_a_transaction_with_no_recovered_payee_keeps_the_one_it_had():
    """
    A later run that could not reach the export must not erase what an earlier
    one recovered.
    """
    row = transaction()
    row.paypal_counterparty = "Spotify AB"

    run(apply([classification()], [row]))

    assert row.paypal_counterparty == "Spotify AB"


def test_applying_nothing_writes_nothing():
    """An import the cache answered entirely still calls this."""
    assert run(apply([], [transaction()])) == 0
