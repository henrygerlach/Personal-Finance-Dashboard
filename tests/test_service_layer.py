"""
Tests for the remaining services: review, recurring, patterns and merchants.

Each of these sits between a detector that is tested on its own and a page,
and each adds the same thing: the user's half of the decision. A correction in
the review queue teaches the merchant cache; a schedule the detector called
finished can be marked still running; a finding can be put away and will stay
away across the recomputation that happens on every load.

What that shares is a keying problem. Nothing here is stored against an id,
because findings and schedules are recomputed from scratch on each request and
an id would not survive it. They are keyed on the facts — merchant, period and
amount; kind, subject and month — and these tests are mostly about whether a
decision still finds the thing it was made about afterwards.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import services as services_module
from app.models import (
    OVERRIDE_ACTIVE,
    OVERRIDE_DISMISSED,
    OVERRIDE_ENDED,
    SOURCE_USER,
)
from app.services import (
    MerchantService,
    PatternService,
    RecurringService,
    ReviewService,
)
from tests.fixtures.odm import document


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch):
    models = SimpleNamespace(
        Transaction=document("Transaction", "id", "minor_category"),
        RecurringOverride=document("RecurringOverride", "merchant_key", "period"),
        PatternDismissal=document("PatternDismissal", "kind", "subject", "month"),
        MerchantCategory=document("MerchantCategory", "merchant_key", "direction"),
    )
    for name in ("Transaction", "RecurringOverride", "PatternDismissal"):
        monkeypatch.setattr(services_module, name, getattr(models, name))
    monkeypatch.setattr(
        "app.classification.service.MerchantCategory", models.MerchantCategory
    )
    return models


def tx(db, date="2026-07-03", amount=-40.0, text="EDEKA", **extra):
    defaults = dict(
        date=datetime.strptime(date, "%Y-%m-%d") if isinstance(date, str) else date,
        amount=amount,
        booking_text=text,
        process=None,
        major_category="Food & Drink",
        minor_category="Groceries",
        flow_type="expense",
        is_recurring=False,
        confidence=0.9,
        reason="",
        classification_source="llm",
        paypal_counterparty=None,
        paypal_settlement_code=None,
    )
    defaults.update(extra)
    row = db.Transaction(**defaults)
    row.save = _saver(row)
    db.Transaction.rows.append(row)
    return row


def _saver(row):
    async def save():
        row.saved = True

    return save


def series(
    db, merchant="NETFLIX", amount=-12.99, count=12, start="2025-08-04", **extra
):
    """A monthly charge, which is what the detector recognises as a schedule."""
    first = datetime.strptime(start, "%Y-%m-%d")
    rows = []
    for index in range(count):
        month = first.month - 1 + index
        when = first.replace(year=first.year + month // 12, month=month % 12 + 1)
        rows.append(tx(db, date=when, amount=amount, text=merchant, **extra))
    return rows


# --- Correcting a transaction by hand -----------------------------------------


def test_a_correction_files_the_transaction_and_marks_it_as_the_user_s(db):
    """
    The confidence goes to 1 and the source to ``user``, which is what makes
    the classifier leave it alone on the next run.
    """
    row = tx(db, major_category="Uncategorized", minor_category="Needs Review")

    corrected, taught = run(
        ReviewService.correct(str(row.id), "Food & Drink", "Groceries")
    )

    assert corrected.major_category == "Food & Drink"
    assert corrected.flow_type == "expense"
    assert corrected.confidence == 1.0
    assert corrected.classification_source == SOURCE_USER
    assert taught is True


def test_a_correction_teaches_the_merchant_cache(db):
    """
    The correction is the point: it settles this transaction and every future
    one from the same merchant running the same way.
    """
    row = tx(db, text="Auftraggeber: EDEKA")

    run(ReviewService.correct(str(row.id), "Food & Drink", "Groceries"))

    assert len(db.MerchantCategory.rows) == 1
    assert db.MerchantCategory.rows[0].source == SOURCE_USER


def test_a_correction_teaches_only_this_transaction_s_direction(db):
    """
    Correcting a payment *to* somebody must not overwrite what was learned
    from the payments received from them, which mean something else entirely.
    """
    row = tx(db, text="MAX MUSTERMANN", amount=-50.0)

    run(ReviewService.correct(str(row.id), "Transfers & Savings", "Person to Person"))

    assert db.MerchantCategory.rows[0].direction == "out"


def test_a_pair_that_is_not_a_category_is_refused_rather_than_coerced(db):
    """
    Silently turning an invalid pair into Needs Review would file the
    transaction as unreviewed *and* teach the cache that answer as if a person
    had chosen it, so one malformed form post poisons the merchant forever.
    """
    row = tx(db)

    with pytest.raises(ValueError, match="is not a category"):
        run(ReviewService.correct(str(row.id), "Invented", "Nonsense"))

    assert db.MerchantCategory.rows == []
    assert row.major_category == "Food & Drink"


def test_correcting_a_transaction_that_is_gone_raises(db):
    with pytest.raises(ValueError, match="No transaction"):
        run(ReviewService.correct("68af1e2c9b4d5a6f7c8d9e01", "Housing", "Rent"))


def test_a_line_with_no_merchant_is_filed_without_teaching_anything(db):
    """
    A truncated PayPal line normalises to nothing. An entry under the empty
    key would then answer for every other one of them.
    """
    row = tx(db, text="PP.5189.PP . , Ihr Ei nkauf bei Ref.")

    _, taught = run(
        ReviewService.correct(str(row.id), "Shopping", "General Merchandise")
    )

    assert taught is False
    assert db.MerchantCategory.rows == []


# --- The queue ----------------------------------------------------------------


def test_the_queue_holds_the_least_confident_first(db):
    """
    The queue is worked from the top, so the transactions the classifier was
    least sure about have to be the ones a person sees first.
    """
    tx(db, minor_category="Needs Review", confidence=0.6, text="SURER")
    tx(db, minor_category="Needs Review", confidence=0.1, text="LEAST SURE")
    tx(db, minor_category="Groceries", confidence=0.99, text="FILED")

    queue = run(ReviewService.queue())

    assert [row.booking_text for row in queue] == ["LEAST SURE", "SURER"]


def test_the_queue_count_is_what_the_badge_shows(db):
    tx(db, minor_category="Needs Review")
    tx(db, minor_category="Needs Review")
    tx(db, minor_category="Groceries")

    assert run(ReviewService.count()) == 2


def test_a_queue_of_ordinary_lines_asks_paypal_nothing(db):
    """
    Most transactions carry no settlement code, and building a ledger for a
    screenful that has none is a read of the whole collection for nothing.
    """
    assert run(ReviewService.paypal_context([tx(db)])) == {}


# --- Recurring: detection and the flag ----------------------------------------


def test_detection_marks_the_transactions_of_a_schedule(db):
    """
    The dashboard draws a "repeats" badge from the flag, so it has to agree
    with the recurring page.
    """
    series(db)

    runs = run(RecurringService.detect_and_store())

    assert len(runs) == 1
    assert all(row.is_recurring for row in db.Transaction.rows)


def test_detection_clears_the_flag_from_what_is_no_longer_a_schedule(db):
    """
    Recomputed rather than updated: a flag left behind contradicts the page it
    came from, and a badge that disagrees is worse than no badge.
    """
    row = tx(db, text="ONE OFF", is_recurring=True)

    run(RecurringService.detect_and_store())

    assert row.is_recurring is False


def test_a_schedule_dismissed_by_hand_does_not_flag_its_transactions(db):
    """
    A dismissed run is not a schedule, so the badge must go with it. This is
    the hand-made decision overruling the detector, which is the whole reason
    overrides exist.
    """
    rows = series(db)
    db.RecurringOverride.rows.append(
        db.RecurringOverride(
            merchant_key="NETFLIX",
            display_name="NETFLIX",
            period="monthly",
            amount=-12.99,
            decision=OVERRIDE_DISMISSED,
            decided_at=datetime(2026, 8, 1),
        )
    )

    kept = run(RecurringService.detect_and_store())

    assert kept == []
    assert not any(row.is_recurring for row in rows)


# --- Recurring: the overview --------------------------------------------------


def test_the_overview_carries_a_schedule_at_its_monthly_equivalent(db):
    """
    A 90 EUR annual insurance is 7.50 a month next to a 4.99 subscription, and
    a total that mixed the two would mean nothing.
    """
    series(db, merchant="NETFLIX", amount=-12.99, count=12)

    overview = run(RecurringService.overview())

    assert overview["outgoing"]
    assert overview["outgoing"][0]["monthly_equivalent"] == pytest.approx(
        -12.99, abs=0.01
    )


def test_now_is_the_end_of_the_imported_history_not_today(db):
    """
    Statements arrive roughly monthly, so between two uploads today runs ahead
    of everything the database knows and every schedule looks abandoned.
    """
    series(db, count=12, start="2025-08-04")

    overview = run(RecurringService.overview())

    assert overview["outgoing"], "a schedule running to the end of the data is live"
    assert overview["days_behind"] >= 0


def test_a_schedule_that_stopped_long_ago_is_not_live(db):
    """
    A subscription cancelled two years ago is still a real pattern in the
    history, and it must not sit in "what am I committed to".
    """
    series(db, merchant="OLD SUB", count=6, start="2024-01-04")
    tx(db, date="2026-07-03", text="SOMETHING RECENT", amount=-5.0)

    overview = run(RecurringService.overview())

    assert not any(item["merchant"] == "OLD SUB" for item in overview["outgoing"])
    assert any(item["merchant"] == "OLD SUB" for item in overview["ended"])


# --- Recurring: overrides -----------------------------------------------------


def schedule_id(db):
    """The handle the page carries for the one detected run."""
    overview = run(RecurringService.overview())
    return overview["items"][0]["id"]


def test_a_decision_is_stored_against_the_facts_of_the_run(db):
    """
    Not against the id: the id is a handle the browser hands back, and the
    merchant, period and amount have to come from the detector instead.
    """
    series(db)

    stored = run(RecurringService.set_override(schedule_id(db), OVERRIDE_ENDED))

    assert stored["decision"] == OVERRIDE_ENDED
    assert db.RecurringOverride.rows[0].period == "monthly"
    assert db.RecurringOverride.rows[0].amount == pytest.approx(-12.99)


def test_deciding_twice_updates_the_one_decision(db):
    """Two rows for one schedule would take two clicks to undo."""
    series(db)
    handle = schedule_id(db)

    run(RecurringService.set_override(handle, OVERRIDE_ENDED))
    run(RecurringService.set_override(handle, OVERRIDE_ACTIVE))

    assert len(db.RecurringOverride.rows) == 1
    assert db.RecurringOverride.rows[0].decision == OVERRIDE_ACTIVE


def test_an_unknown_decision_is_refused(db):
    series(db)

    with pytest.raises(ValueError, match="Unknown decision"):
        run(RecurringService.set_override(schedule_id(db), "maybe"))


def test_a_decision_about_a_schedule_detection_no_longer_finds_is_refused(db):
    """
    Findings are recomputed on every load. Storing an override for a run that
    no longer exists leaves a row nothing can ever match or clear.
    """
    series(db)

    with pytest.raises(ValueError, match="No detected schedule"):
        run(RecurringService.set_override("nonexistent-monthly", OVERRIDE_ENDED))


def test_a_decision_can_be_handed_back_to_the_detector(db):
    series(db)
    handle = schedule_id(db)
    run(RecurringService.set_override(handle, OVERRIDE_ENDED))

    assert run(RecurringService.clear_override(handle)) is True
    assert db.RecurringOverride.rows == []


def test_clearing_a_decision_that_was_never_made_says_so(db):
    series(db)

    assert run(RecurringService.clear_override(schedule_id(db))) is False


def test_clearing_a_decision_about_an_unknown_schedule_is_refused(db):
    series(db)

    with pytest.raises(ValueError, match="No detected schedule"):
        run(RecurringService.clear_override("nonexistent-monthly"))


# --- Patterns: putting a finding away -----------------------------------------


def test_a_finding_is_dismissed_on_its_kind_subject_and_month(db):
    """
    Findings are rebuilt from scratch on every page load, so the dismissal has
    to be keyed on what the finding is rather than on any handle.
    """
    assert run(PatternService.dismiss("drift", "Cafés & Snacks", "2026-07")) is True

    stored = db.PatternDismissal.rows[0]
    assert (stored.kind, stored.subject, stored.month) == (
        "drift",
        "Cafés & Snacks",
        "2026-07",
    )


def test_dismissing_the_same_finding_twice_stores_one_row(db):
    run(PatternService.dismiss("drift", "Cafés & Snacks", "2026-07"))

    assert run(PatternService.dismiss("drift", "Cafés & Snacks", "2026-07")) is False
    assert len(db.PatternDismissal.rows) == 1


def test_the_same_subject_in_a_different_month_is_a_different_finding(db):
    """
    The same category drifting again next year is a new claim about a new
    month, and is worth offering again.
    """
    run(PatternService.dismiss("drift", "Cafés & Snacks", "2026-07"))

    assert run(PatternService.dismiss("drift", "Cafés & Snacks", "2027-07")) is True


def test_a_kind_this_app_does_not_produce_is_refused(db):
    """A dismissal matching nothing would sit in the collection forever."""
    with pytest.raises(ValueError, match="is not a kind of pattern"):
        run(PatternService.dismiss("invented", "", ""))


def test_a_dismissed_finding_can_be_restored(db):
    run(PatternService.dismiss("drift", "Cafés & Snacks", "2026-07"))

    assert run(PatternService.restore("drift", "Cafés & Snacks", "2026-07")) is True
    assert db.PatternDismissal.rows == []


def test_restoring_a_finding_that_was_never_dismissed_says_so(db):
    assert run(PatternService.restore("drift", "Cafés & Snacks", "2026-07")) is False


# --- Merchants ----------------------------------------------------------------


def test_the_merchants_page_of_an_empty_ledger_renders(db):
    """A fresh install reaches every page before anything is uploaded."""
    overview = run(MerchantService.overview())

    assert overview["months"] == []
    assert overview["first"] == ""


def test_a_merchant_row_carries_the_payments_behind_it(db):
    """
    The row claims who was paid, and a list arriving in a second request is a
    list that can be read after the claim it was meant to support.
    """
    for month in range(1, 13):
        tx(db, date=f"2025-{month:02d}-05", amount=-40.0, text="EDEKA MUENCHEN")
    tx(db, date="2026-01-05", amount=-40.0, text="EDEKA MUENCHEN")

    overview = run(MerchantService.overview())

    assert overview["months"]
    assert overview["merchants"]
    assert overview["merchants"][0]["rows"]
