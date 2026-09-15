"""
Tests for the review queue, the correction path and the recurring overview.

The correction is the piece that has to be right: it settles one transaction
and teaches the merchant cache, so a bad correction spreads.
"""

import calendar
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import RecurringService, ReviewService
from app.utils import should_exclude_transaction
from tests.fixtures.odm import patch_services


def tx(tid="1", major="Uncategorized", minor="Needs Review", **extra):
    defaults = dict(
        id=tid,
        date=datetime(2026, 8, 26),
        amount=-10.0,
        booking_text=" NYA HERING SANIKONZEPT ",
        process=None,
        major_category=major,
        minor_category=minor,
        flow_type=None,
        confidence=0.4,
        reason="unsure",
        classification_source="llm",
        is_recurring=False,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


# --- The exclusion rule ------------------------------------------------------


def test_transfers_are_excluded_by_flow_type():
    """
    The old rule matched two minor-category names and let real money through:
    transfers to other people counted as income, and securities purchases and
    cash withdrawals counted as spending.
    """
    assert should_exclude_transaction(
        tx(major="Transfers & Savings", minor="Investments", flow_type="transfer")
    )
    assert not should_exclude_transaction(
        tx(major="Food & Drink", minor="Groceries", flow_type="expense")
    )
    assert not should_exclude_transaction(
        tx(major="Income", minor="Salary & Wages", flow_type="income")
    )


def test_a_transaction_from_before_the_migration_falls_back_to_the_old_rule():
    """The dashboard has to keep working on a half-migrated database."""
    assert should_exclude_transaction(
        tx(major="Transfers", minor="Internal", flow_type=None)
    )
    assert not should_exclude_transaction(
        tx(major="Food & Drinks", minor="Groceries", flow_type=None)
    )


def test_the_bare_name_form_still_works():
    """Some callers only have the minor category to hand."""
    assert should_exclude_transaction("Internal")
    assert not should_exclude_transaction("Groceries")


# --- Corrections -------------------------------------------------------------


def _patch_correct(monkeypatch, transaction, remembered):
    async def get(_id):
        return transaction

    async def save():
        return None

    async def remember(name, major, minor, source, direction, **kwargs):
        remembered.append((name, major, minor, source, direction))

    transaction.save = save
    patch_services(
        monkeypatch,
        Transaction=SimpleNamespace(get=get),
        object_id=lambda value: value,
    )
    import app.classification.service as classification_service

    monkeypatch.setattr(classification_service, "remember_merchant", remember)


def test_a_correction_files_the_transaction_and_teaches_the_merchant(
    monkeypatch,
):
    """
    This is the point of the queue: one decision settles this transaction and
    every future one from the same merchant, without another API call.
    """
    transaction = tx()
    remembered = []
    _patch_correct(monkeypatch, transaction, remembered)

    import asyncio

    updated, cached = asyncio.run(
        ReviewService.correct("1", "Housing", "Maintenance & Services")
    )

    assert (updated.major_category, updated.minor_category) == (
        "Housing",
        "Maintenance & Services",
    )
    assert updated.flow_type == "expense"
    assert updated.confidence == 1.0
    assert updated.classification_source == "user"
    assert cached is True
    assert remembered == [
        ("NYA HERING SANIKONZEPT", "Housing", "Maintenance & Services", "user", "out")
    ]


def test_an_invalid_pair_is_refused_rather_than_coerced(monkeypatch):
    """
    Coercing it would file the transaction as unreviewed *and* teach the
    merchant cache that answer as if a person had chosen it, so one malformed
    form post would poison every future transaction from that merchant. This
    happened — a form value containing an ampersand arrived split in two.
    """
    transaction = tx()
    remembered = []
    _patch_correct(monkeypatch, transaction, remembered)

    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(ReviewService.correct("1", "Shopping", "Rent"))
    assert remembered == [], "nothing may be cached from a refused correction"


def test_a_correction_on_a_merchantless_line_reports_that_nothing_was_cached(
    monkeypatch,
):
    """A truncated PayPal line has no merchant to key the lesson on."""
    transaction = tx(
        booking_text=(
            "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. "
            "Buchungstext: 102417739 PP.5189.PP . , Ihr Ei nkauf bei Ref. J8/1"
        )
    )
    remembered = []
    _patch_correct(monkeypatch, transaction, remembered)

    import asyncio

    _, cached = asyncio.run(ReviewService.correct("1", "Housing", "Rent"))
    assert cached is False
    assert remembered == []


# --- The recurring overview --------------------------------------------------


def test_transfers_are_kept_out_of_the_recurring_totals(monkeypatch):
    """
    A monthly savings plan is not an expense and a standing order between own
    accounts is not income — the same rule the charts follow. They are still
    listed, because a 500-a-month transfer is worth seeing.
    """
    start = datetime(2025, 1, 10)

    def charge(tid, i, amount, merchant, major, minor, flow):
        return tx(
            tid=tid,
            major=major,
            minor=minor,
            flow_type=flow,
            amount=amount,
            booking_text=f" {merchant} ",
            date=start + timedelta(days=round(i * 30.4)),
        )

    transactions = []
    for i in range(4):
        transactions.append(
            charge(
                f"s{i}",
                i,
                -50.0,
                "SPOTIFY",
                "Leisure & Entertainment",
                "Streaming & Media",
                "expense",
            )
        )
        transactions.append(
            charge(
                f"i{i}",
                i,
                -500.0,
                "BROKER",
                "Transfers & Savings",
                "Investments",
                "transfer",
            )
        )

    overview = _overview(monkeypatch, transactions)

    assert overview["count"] == 2, "both schedules are listed"
    assert overview["monthly_expense"] == pytest.approx(-50.0)
    assert overview["monthly_transfer"] == pytest.approx(-500.0)
    assert overview["transfer_count"] == 1


def test_yearly_charges_are_shown_at_their_monthly_equivalent(monkeypatch):
    """
    Otherwise the total is meaningless: a 90 EUR annual premium would sit next
    to a 4.99 subscription as if it cost twenty times as much every month.
    """
    start = datetime(2023, 3, 1)
    transactions = [
        tx(
            tid=f"y{i}",
            major="Financial",
            minor="Insurance",
            flow_type="expense",
            amount=-90.0,
            booking_text=" INSURER ",
            date=start + timedelta(days=365 * i),
        )
        for i in range(3)
    ]

    overview = _overview(monkeypatch, transactions)
    assert overview["items"][0]["period"] == "yearly"
    assert overview["items"][0]["monthly_equivalent"] == pytest.approx(-7.5)
    assert overview["monthly_expense"] == pytest.approx(-7.5)


# --- Liveness ----------------------------------------------------------------
#
# The page this backs answers "what am I committed to right now", so the split
# that has to be right is not income against spending but live against
# finished. Everything below is about that decision.


def _months_before(moment, months):
    """The same day of the month, ``months`` earlier."""
    total = moment.month - 1 - months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _series(prefix, merchant, amount, count, last, major, minor, flow, months=1):
    """
    ``count`` charges one period apart, the last of them on ``last``.

    Spaced by calendar months rather than by a fixed number of days, because
    that is what a direct debit actually does — and the day of the month it
    lands on is one of the things the page reports.
    """
    return [
        tx(
            tid=f"{prefix}{i}",
            major=major,
            minor=minor,
            flow_type=flow,
            amount=amount,
            booking_text=f" {merchant} ",
            date=_months_before(last, (count - 1 - i) * months),
        )
        for i in range(count)
    ]


def _overview(monkeypatch, transactions, overrides=()):
    """
    Run the overview against a fixed set of transactions and hand-made
    decisions, with no database behind either.
    """

    class Query:
        def __init__(self, rows):
            self.rows = rows

        async def to_list(self):
            return list(self.rows)

    patch_services(
        monkeypatch,
        Transaction=SimpleNamespace(find_all=lambda: Query(transactions)),
    )
    patch_services(
        monkeypatch,
        RecurringOverride=SimpleNamespace(find_all=lambda: Query(overrides)),
    )
    import asyncio

    return asyncio.run(RecurringService.overview())


def _override(merchant, decision, amount, period="monthly"):
    """One stored decision, as the overview reads it."""
    return SimpleNamespace(
        merchant_key=merchant.replace(" ", ""),
        display_name=merchant,
        period=period,
        amount=amount,
        decision=decision,
    )


#: Every fixture below is anchored in the past, so "the end of the history" is
#: a date the test controls rather than whenever the suite happens to run.
NOW = datetime(2026, 1, 20)


def _rent(last=NOW):
    """A schedule that is unambiguously still running, to anchor the history."""
    return _series("rent", "MIETE", -1100.0, 12, last, "Housing", "Rent", "expense")


def test_now_is_the_end_of_the_history_not_today(monkeypatch):
    """
    Statements are uploaded in batches, so between two uploads today runs
    ahead of everything the database knows. Measuring liveness against today
    would then report every schedule as abandoned the moment an import fell a
    month behind.
    """
    overview = _overview(monkeypatch, _rent())

    assert overview["reference_date"] == "2026-01-20"
    assert overview["items"][0]["status"] == "active"


def test_a_schedule_that_stopped_is_not_a_commitment(monkeypatch):
    """
    A subscription cancelled a year ago is still a real pattern in the
    history, but putting it in "recurring out per month" is a lie about the
    future — which is the whole reason the totals and the layout split on
    liveness rather than on direction.
    """
    cancelled = _series(
        "nf",
        "NETFLIX",
        -12.99,
        6,
        NOW - timedelta(days=365),
        "Leisure & Entertainment",
        "Streaming & Media",
        "expense",
    )
    overview = _overview(monkeypatch, _rent() + cancelled)

    ended = [item for item in overview["items"] if item["merchant"] == "NETFLIX"]
    assert ended and ended[0]["status"] == "ended"
    assert overview["ended_count"] == 1
    assert overview["monthly_expense"] == pytest.approx(
        -1100.0
    ), "the cancelled subscription must not be in the total"
    assert [item["merchant"] for item in overview["outgoing"]] == ["MIETE"]


def test_a_charge_that_is_merely_late_is_due_rather_than_ended(monkeypatch):
    """
    A direct debit that slid over a long weekend has not been cancelled. The
    distinction earns its keep in the other direction too: a schedule that has
    quietly stopped billing is worth noticing, and it can only be noticed once
    "late" and "over" are different states.
    """
    late = _series(
        "gym",
        "FITNESSSTUDIO",
        -29.9,
        6,
        NOW - timedelta(days=34),
        "Health & Personal Care",
        "Gym & Fitness",
        "expense",
    )
    overview = _overview(monkeypatch, _rent() + late)

    gym = next(i for i in overview["items"] if i["merchant"] == "FITNESSSTUDIO")
    assert gym["status"] == "due"
    assert gym["days_away"] < 0
    assert overview["overdue_count"] == 1
    # Still a commitment: it is late, not gone.
    assert gym in overview["outgoing"]


def test_lateness_is_measured_against_the_schedules_own_interval(monkeypatch):
    """
    A monthly charge four weeks late has stopped; a yearly one four weeks late
    is simply late. A single grace period in days would have to be wrong for
    one of them.
    """
    yearly = _series(
        "ins",
        "ALLIANZ",
        -240.0,
        4,
        NOW - timedelta(days=365 + 30),
        "Financial",
        "Insurance",
        "expense",
        months=12,
    )
    overview = _overview(monkeypatch, _rent() + yearly)

    insurance = next(i for i in overview["items"] if i["merchant"] == "ALLIANZ")
    assert insurance["status"] == "due"


# --- What the page says about a schedule -------------------------------------


def test_the_next_charge_is_projected_onto_the_day_it_usually_lands(monkeypatch):
    """
    "Next on the 20th" is what a monthly schedule is actually recognised by,
    and it is what makes the thirty-day strip mean anything.
    """
    overview = _overview(monkeypatch, _rent())

    rent = overview["items"][0]
    assert rent["typical_day"] == 20
    assert rent["next_expected"] == "2026-02-20"
    assert rent["days_away"] == 31


def test_a_price_rise_inside_a_schedule_is_reported(monkeypatch):
    """
    Detection tolerates fifteen percent of drift, which is what keeps a
    streaming price rise from splitting into two schedules — but it also means
    the rise then hides inside the median and would never be shown.
    """
    charges = _series(
        "sp",
        "SPOTIFY",
        -9.99,
        12,
        NOW,
        "Leisure & Entertainment",
        "Streaming & Media",
        "expense",
    )
    for charge in charges[6:]:
        charge.amount = -10.99

    overview = _overview(monkeypatch, _rent() + charges)

    spotify = next(i for i in overview["items"] if i["merchant"] == "SPOTIFY")
    assert spotify["price_change"] == {
        "from": -9.99,
        "to": -10.99,
        "changed_on": "2025-08",
    }
    assert overview["price_change_count"] == 1
    # The page is a statement of what is owed now, so the schedule is carried
    # at the new price rather than at the average of the two.
    assert spotify["amount"] == pytest.approx(-10.99)


def test_a_steady_schedule_reports_no_price_change(monkeypatch):
    """A variable bill wobbling by a few cents has not changed price."""
    charges = _rent()
    charges[3].amount = -1105.0
    overview = _overview(monkeypatch, charges)

    assert overview["items"][0]["price_change"] is None
    assert overview["price_change_count"] == 0


# --- What is coming ----------------------------------------------------------


def test_the_strip_carries_transfers_even_though_the_totals_do_not(monkeypatch):
    """
    The totals are about earning and spending, where a savings plan is
    neither. The strip is about what leaves the account and when, where it
    very much counts.
    """
    savings = _series(
        "sv",
        "DEPOT SPARPLAN",
        -500.0,
        12,
        NOW - timedelta(days=25),
        "Transfers & Savings",
        "Investments",
        "transfer",
    )
    overview = _overview(monkeypatch, _rent() + savings)

    assert overview["monthly_transfer"] == pytest.approx(-500.0)
    assert overview["monthly_expense"] == pytest.approx(-1100.0)
    assert "DEPOT SPARPLAN" in [item["merchant"] for item in overview["upcoming"]]


def test_a_schedule_that_has_stopped_is_not_expected_again(monkeypatch):
    """Nothing is coming from a schedule that is over."""
    cancelled = _series(
        "nf",
        "NETFLIX",
        -12.99,
        6,
        NOW - timedelta(days=400),
        "Leisure & Entertainment",
        "Streaming & Media",
        "expense",
    )
    overview = _overview(monkeypatch, _rent() + cancelled)

    assert "NETFLIX" not in [item["merchant"] for item in overview["upcoming"]]


def test_the_committed_share_is_withheld_without_recurring_income(monkeypatch):
    """
    A share of nothing is not zero, it is unanswerable — and shown as a bar it
    would read as "nothing is committed", which is the opposite of the truth.
    """
    assert _overview(monkeypatch, _rent())["committed_share"] is None


def test_the_committed_share_measures_spending_against_recurring_income(
    monkeypatch,
):
    salary = _series(
        "sal", "ARBEITGEBER", 2200.0, 12, NOW, "Income", "Salary & Wages", "income"
    )
    overview = _overview(monkeypatch, _rent() + salary)

    assert overview["committed_share"] == pytest.approx(0.5)
    assert overview["annual_expense"] == pytest.approx(-13200.0)


def test_a_rise_late_in_a_long_history_is_still_reported(monkeypatch):
    """
    Rent held at 405 for three years and then raised to 447 leaves both halves
    of the history with a median of 405. Reading the current price off the end
    instead finds the raise that is still being paid every month.
    """
    charges = _series(
        "rt", "STUDIERENDENWERK", -405.0, 47, NOW, "Housing", "Rent", "expense"
    )
    for charge in charges[-11:]:
        charge.amount = -447.0

    overview = _overview(monkeypatch, charges)

    assert overview["items"][0]["price_change"] == {
        "from": -405.0,
        "to": -447.0,
        "changed_on": "2025-03",
    }
    assert overview["items"][0]["amount"] == pytest.approx(-447.0)
    assert overview["monthly_expense"] == pytest.approx(
        -447.0
    ), "the lifetime median would understate a bill still being paid in full"


def test_one_odd_month_is_not_a_price_change(monkeypatch):
    """
    A single charge at a different size is an anomaly, not a new price — and
    since the current price is read off the end, the newest charge is exactly
    where an anomaly would do the most damage.
    """
    charges = _rent()
    charges[-1].amount = -1400.0

    overview = _overview(monkeypatch, charges)
    assert overview["items"][0]["price_change"] is None
    assert overview["items"][0]["amount"] == pytest.approx(
        -1100.0
    ), "and the anomaly must not become the schedule's price either"


def test_a_schedule_broken_by_one_missed_month_is_still_running(monkeypatch):
    """
    The end-to-end version of the detector's own test, because this is where
    it was noticed: a rent still being paid every month was shown under
    "schedules that stopped", dated to whatever month its run happened to
    break.
    """
    charges = _series("lf", "VERMIETER", -399.0, 9, NOW, "Housing", "Rent", "expense")
    del charges[5]

    overview = _overview(monkeypatch, charges)

    schedule = overview["items"][0]
    assert schedule["status"] == "active"
    assert schedule["last_seen"] == "2026-01-20"
    assert overview["ended_count"] == 0


def test_the_page_reports_how_far_behind_the_history_is(monkeypatch):
    """
    Statements are uploaded in batches, so every "next in N days" on the page
    counts from the end of the history rather than from today. That only means
    something if the page also says how far back the end of the history is.
    """
    overview = _overview(monkeypatch, _rent(last=datetime.now() - timedelta(days=27)))

    assert overview["days_behind"] == 27


def test_a_history_that_reaches_today_is_not_behind(monkeypatch):
    assert _overview(monkeypatch, _rent(last=datetime.now()))["days_behind"] == 0


# --- Hand-made decisions -----------------------------------------------------
#
# Detection reads a pattern out of dates and amounts, so it can only guess at
# two things: whether the pattern is a schedule at all, and whether it is
# still running. Neither can be fixed by tuning a threshold without breaking
# the other case, so the user has the last word.


def test_a_dismissed_schedule_leaves_the_page(monkeypatch):
    """
    Three bakery visits a month apart are not a subscription. Dismissing one
    has to take it out of the totals too, not merely out of the list: a
    commitment the user has said is not a commitment must not be added up.
    """
    bakery = _series(
        "bk",
        "BAECKEREI",
        -2.4,
        3,
        NOW,
        "Food & Drink",
        "Cafés & Snacks",
        "expense",
    )
    overview = _overview(
        monkeypatch,
        _rent() + bakery,
        overrides=[_override("BAECKEREI", "dismissed", -2.4)],
    )

    assert [item["merchant"] for item in overview["outgoing"]] == ["MIETE"]
    assert overview["monthly_expense"] == pytest.approx(-1100.0)
    assert overview["dismissed_count"] == 1
    assert overview["ended_count"] == 0, "dismissed is not the same as stopped"
    # Still reachable, because a decision has to be reversible.
    assert [item["merchant"] for item in overview["dismissed"]] == ["BAECKEREI"]


def test_a_schedule_can_be_marked_still_running(monkeypatch):
    """
    The user knows what the dates cannot show. A schedule paused over the
    summer looks finished and is not.
    """
    paused = _series(
        "gy",
        "FITNESS",
        -29.9,
        6,
        NOW - timedelta(days=200),
        "Health & Personal Care",
        "Gym & Fitness",
        "expense",
    )
    overview = _overview(
        monkeypatch,
        _rent() + paused,
        overrides=[_override("FITNESS", "active", -29.9)],
    )

    gym = next(i for i in overview["items"] if i["merchant"] == "FITNESS")
    assert gym["detected_status"] == "ended", "detection's own answer is kept"
    assert gym["status"] == "active"
    assert gym in overview["outgoing"]
    assert overview["monthly_expense"] == pytest.approx(-1129.9)


def test_a_schedule_marked_running_is_expected_next_from_now(monkeypatch):
    """
    Rolled forward to the next occurrence that has not already passed:
    "expected 200 days ago" is not a useful thing to say about a payment the
    user has just confirmed is alive.
    """
    paused = _series(
        "gy",
        "FITNESS",
        -29.9,
        6,
        NOW - timedelta(days=200),
        "Health & Personal Care",
        "Gym & Fitness",
        "expense",
    )
    overview = _overview(
        monkeypatch,
        _rent() + paused,
        overrides=[_override("FITNESS", "active", -29.9)],
    )

    gym = next(i for i in overview["items"] if i["merchant"] == "FITNESS")
    assert gym["days_away"] >= 0
    assert gym["next_expected"] > overview["reference_date"]


def test_a_schedule_can_be_marked_finished(monkeypatch):
    """The other direction: cancelled last week, still charging on paper."""
    overview = _overview(
        monkeypatch, _rent(), overrides=[_override("MIETE", "ended", -1100.0)]
    )

    assert overview["items"][0]["detected_status"] == "active"
    assert overview["items"][0]["status"] == "ended"
    assert overview["monthly_expense"] == 0
    assert overview["ended_count"] == 1


def test_a_decision_survives_a_price_rise(monkeypatch):
    """
    An override is matched on the amount within the detector's own tolerance,
    which is the same tolerance that keeps a price rise inside one run. A
    decision made before the rise therefore still speaks for the schedule
    after it — otherwise every raised subscription would quietly revert.
    """
    charges = _rent()
    for charge in charges[-4:]:
        charge.amount = -1180.0

    overview = _overview(
        monkeypatch,
        charges,
        # The price when the decision was made, not the price now.
        overrides=[_override("MIETE", "dismissed", -1100.0)],
    )
    assert overview["dismissed_count"] == 1


def test_a_decision_does_not_reach_a_different_schedule(monkeypatch):
    """
    One merchant can run two schedules at the same period, told apart only by
    amount — two standing orders from one payer. Dismissing one must not
    dismiss the other.
    """
    small = _series("a", "PAYER", 200.0, 6, NOW, "Income", "Other Income", "income")
    large = _series("b", "PAYER", 900.0, 6, NOW, "Income", "Other Income", "income")
    overview = _overview(
        monkeypatch, small + large, overrides=[_override("PAYER", "dismissed", 200.0)]
    )

    assert overview["dismissed_count"] == 1
    assert [item["amount"] for item in overview["incoming"]] == [900.0]


def test_a_dismissed_schedule_does_not_flag_its_transactions(monkeypatch):
    """
    The dashboard draws a "repeats" badge from ``is_recurring``, so a badge
    that contradicts the recurring page is worse than no badge at all: the
    flag has to respect the same decision the page does.
    """
    bakery = _series(
        "bk",
        "BAECKEREI",
        -2.4,
        3,
        NOW,
        "Food & Drink",
        "Cafés & Snacks",
        "expense",
    )
    transactions = _rent() + bakery
    for transaction in transactions:
        transaction.save = _record_save(transaction)

    class Query:
        def __init__(self, rows):
            self.rows = rows

        async def to_list(self):
            return list(self.rows)

    patch_services(
        monkeypatch,
        Transaction=SimpleNamespace(find_all=lambda: Query(transactions)),
        RecurringOverride=SimpleNamespace(
            find_all=lambda: Query([_override("BAECKEREI", "dismissed", -2.4)])
        ),
    )

    import asyncio

    kept = asyncio.run(RecurringService.detect_and_store())

    assert [run.merchant for run in kept] == ["MIETE"]
    assert all(t.is_recurring for t in transactions if "MIETE" in t.booking_text)
    assert not any(
        t.is_recurring for t in transactions if "BAECKEREI" in t.booking_text
    )


def _record_save(transaction):
    """A stand-in for Beanie's save that keeps the change in memory."""

    async def save():
        transaction.saved = True

    return save


def test_a_correction_against_an_id_that_is_not_an_id_is_a_value_error(monkeypatch):
    """
    The route turns a ValueError from here into a 422. Beanie's id parser
    raises bson's InvalidId instead, which descends from BSONError rather than
    ValueError, so before it was funnelled through object_id a mistyped id in
    the form post left as a 500.
    """
    import asyncio

    def unreachable(*args):
        pytest.fail("the id should not have reached the database")

    patch_services(monkeypatch, Transaction=SimpleNamespace(get=unreachable))

    with pytest.raises(ValueError):
        asyncio.run(ReviewService.correct("not-an-id", "Food & Drink", "Groceries"))


# --- Two charges in a row ------------------------------------------------------
#
# The one case detection cannot settle on its own: the second month of a
# subscription and two restaurant visits a month apart are the same shape.


def _pair(merchant="FITNESSSTUDIO", amount=-29.9, last=NOW):
    """Two charges in consecutive months, which is not yet a schedule."""
    return _series(
        "pr", merchant, amount, 2, last, "Health & Fitness", "Gym & Sports", "expense"
    )


def test_two_charges_in_a_row_are_asked_about_rather_than_counted(monkeypatch):
    """
    A pair is a question, so it may not quietly join the standing
    commitments: putting 29,90 into "going out per month" on the strength of
    two charges states a commitment nobody has made.
    """
    overview = _overview(monkeypatch, _rent() + _pair())

    assert overview["candidate_count"] == 1
    assert [item["merchant"] for item in overview["candidates"]] == ["FITNESSSTUDIO"]
    assert overview["monthly_expense"] == -1100.0
    assert [item["merchant"] for item in overview["outgoing"]] == ["MIETE"]


def test_two_charges_of_different_sizes_are_not_worth_asking_about(monkeypatch):
    """
    A schedule of three or more may drift, because its rhythm is evidence in
    its own right. Two charges have nothing but their size, so 29,90 followed
    by 27,50 is a coincidence rather than a bill — and asking about every one
    of those buries the pairs that are worth an answer.
    """
    pair = _pair()
    pair[0].amount = -27.5
    overview = _overview(monkeypatch, _rent() + pair)

    assert overview["candidate_count"] == 0


def test_a_confirmed_pair_becomes_a_commitment_like_any_other(monkeypatch):
    """
    Confirming is the whole point of asking: once the user says it recurs it
    belongs in the totals, the columns and the strip, with no trace of having
    been a candidate.
    """
    overview = _overview(
        monkeypatch,
        _rent() + _pair(),
        overrides=[_override("FITNESSSTUDIO", "active", -29.9)],
    )

    assert overview["candidate_count"] == 0
    assert overview["monthly_expense"] == -1129.9
    assert "FITNESSSTUDIO" in [item["merchant"] for item in overview["outgoing"]]


def test_a_dismissed_pair_leaves_the_page_like_a_dismissed_schedule(monkeypatch):
    """
    "No, those two were a coincidence" has to survive, or the page asks the
    same question again after every upload.
    """
    overview = _overview(
        monkeypatch,
        _rent() + _pair(),
        overrides=[_override("FITNESSSTUDIO", "dismissed", -29.9)],
    )

    assert overview["candidate_count"] == 0
    assert [item["merchant"] for item in overview["dismissed"]] == ["FITNESSSTUDIO"]


def test_a_pair_inside_a_longer_history_does_not_rewrite_its_period(monkeypatch):
    """
    A candidate stops at the first period that fits, so looking for pairs in
    the same pass would find the two charges a month apart at the start of a
    quarterly premium and never examine it as quarterly — halving the reported
    interval and tripling what the page says it costs. Pairs are therefore
    only looked for in what full detection did not already claim, which also
    keeps a charge that already belongs to a schedule from being asked about
    as though it were loose.
    """
    premium = _series(
        "q",
        "VERSICHERUNG",
        -120.0,
        3,
        NOW,
        "Insurance",
        "Other Insurance",
        "expense",
        months=3,
    )
    stray = tx(
        tid="qstray",
        major="Insurance",
        minor="Other Insurance",
        flow_type="expense",
        amount=-120.0,
        booking_text=" VERSICHERUNG ",
        date=_months_before(premium[0].date, 1),
    )
    overview = _overview(monkeypatch, _rent() + premium + [stray])

    insurance = [
        item for item in overview["items"] if item["merchant"] == "VERSICHERUNG"
    ]
    assert [item["period"] for item in insurance] == ["quarterly"]
    assert overview["candidate_count"] == 0


def test_an_unconfirmed_pair_does_not_flag_its_transactions(monkeypatch):
    """
    The dashboard's "repeats" badge is drawn from ``is_recurring``, and until
    the question is answered nobody has said these two charges repeat.
    """
    transactions = _rent() + _pair()
    for transaction in transactions:
        transaction.save = _record_save(transaction)

    class Query:
        def __init__(self, rows):
            self.rows = rows

        async def to_list(self):
            return list(self.rows)

    patch_services(
        monkeypatch,
        Transaction=SimpleNamespace(find_all=lambda: Query(transactions)),
        RecurringOverride=SimpleNamespace(find_all=lambda: Query([])),
    )

    import asyncio

    kept = asyncio.run(RecurringService.detect_and_store())

    assert [run.merchant for run in kept] == ["MIETE"]
    assert not any(t.is_recurring for t in transactions if "FITNESS" in t.booking_text)
