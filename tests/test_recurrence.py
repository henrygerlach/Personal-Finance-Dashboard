"""
Tests for recurring-payment detection.

This replaces the old "Subscriptions" category. Whether something is a
subscription is a property of the payment history, not of what the merchant
sells, so none of this involves the classifier.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.recurrence import (
    find_recurring,
    recurring_transaction_ids,
)

START = datetime(2025, 1, 15)


def charge(tid, day_offset, amount, merchant="SPOTIFY AB"):
    return SimpleNamespace(
        id=tid,
        date=START + timedelta(days=day_offset),
        amount=amount,
        booking_text=f" {merchant} ",
        process="Lastschrift / Belastung",
    )


def monthly(merchant, amount, count, start_id=0, jitter=()):
    """``count`` charges roughly a month apart."""
    offsets = [round(i * 30.4) for i in range(count)]
    for index, extra in enumerate(jitter):
        offsets[index] += extra
    return [
        charge(f"{merchant}-{start_id + i}", offset, amount, merchant)
        for i, offset in enumerate(offsets)
    ]


# --- What must be found ------------------------------------------------------


def test_a_monthly_subscription_is_found():
    runs = find_recurring(monthly("SPOTIFY AB", -4.99, 6))
    assert len(runs) == 1
    assert runs[0].period == "monthly"
    assert runs[0].occurrences == 6
    assert runs[0].median_amount == pytest.approx(-4.99)


def test_a_few_days_of_drift_is_tolerated():
    """Direct debits move around weekends and bank holidays."""
    runs = find_recurring(monthly("SPOTIFY AB", -4.99, 5, jitter=(0, 3, -4, 2, -3)))
    assert len(runs) == 1
    assert runs[0].occurrences == 5


def test_a_small_price_rise_stays_one_run():
    charges = monthly("SPOTIFY AB", -4.99, 3)
    charges += [
        charge("late-1", 90, -5.49, "SPOTIFY AB"),
        charge("late-2", 121, -5.49, "SPOTIFY AB"),
        charge("late-3", 151, -5.49, "SPOTIFY AB"),
    ]
    runs = find_recurring(charges)
    assert len(runs) == 1, "10% is within tolerance, so it is still one schedule"
    assert runs[0].occurrences == 6


def test_a_euro_on_a_cheap_subscription_stays_one_run():
    """
    Price rises are not proportional to price. A euro is a rounding error on
    the rent and a fifth of a streaming plan, so at a flat fifteen percent the
    cheap service that got a euro dearer left its own run — and with three
    charges on each side of the rise it was then found as no schedule at all.
    """
    charges = monthly("SPOTIFY AB", -4.99, 3)
    charges += [
        charge("up-1", 90, -5.99, "SPOTIFY AB"),
        charge("up-2", 121, -5.99, "SPOTIFY AB"),
        charge("up-3", 151, -5.99, "SPOTIFY AB"),
    ]
    runs = find_recurring(charges)
    assert len(runs) == 1, "20% of 4.99 is a price rise, not a different payment"
    assert runs[0].occurrences == 6


def test_the_wider_tolerance_stops_where_the_amounts_stop_being_small():
    """
    The widening is for small amounts only: twenty percent of a large bill is
    a different bill, and letting it through would carry two standing orders
    as one and report the wrong commitment.
    """
    charges = monthly("STADTWERKE", -100.00, 3)
    charges += [
        charge("up-1", 90, -120.00, "STADTWERKE"),
        charge("up-2", 121, -120.00, "STADTWERKE"),
        charge("up-3", 151, -120.00, "STADTWERKE"),
    ]
    runs = find_recurring(charges)
    assert {round(run.median_amount) for run in runs} == {-100, -120}


def test_a_refund_is_not_the_charge_it_reverses():
    """
    Sizes used to be compared without their sign, so a 133,65 purchase and the
    133,65 refunded a month later were the same amount — and a shop that
    refunded one order was reported as a standing commitment whose price was
    the median of the two, zero.
    """
    charges = monthly("ADIDAS AG", -133.65, 4)
    charges.append(charge("refund", 45, 133.65, "ADIDAS AG"))
    runs = find_recurring(charges)
    assert len(runs) == 1
    assert runs[0].occurrences == 4
    assert runs[0].median_amount == pytest.approx(-133.65)
    assert "refund" not in runs[0].transaction_ids


def test_a_doubling_is_a_different_payment():
    charges = monthly("GYM", -20.00, 3, start_id=0)
    charges += [
        charge("big-1", 91, -60.00, "GYM"),
        charge("big-2", 121, -60.00, "GYM"),
        charge("big-3", 152, -60.00, "GYM"),
    ]
    runs = find_recurring(charges)
    assert len(runs) == 2
    assert {round(run.median_amount) for run in runs} == {-20, -60}


def test_a_yearly_payment_is_found():
    charges = [
        charge("y1", 0, -89.00, "INSURANCE"),
        charge("y2", 365, -89.00, "INSURANCE"),
        charge("y3", 730, -92.00, "INSURANCE"),
    ]
    runs = find_recurring(charges)
    assert len(runs) == 1
    assert runs[0].period == "yearly"


def test_a_half_yearly_payment_is_found():
    """
    German insurance and membership fees are commonly billed twice a year.
    Before the half-yearly period existed these fell between quarterly and
    yearly and were found as neither.
    """
    charges = [
        charge("h1", 0, -240.00, "ALLIANZ"),
        charge("h2", 182, -240.00, "ALLIANZ"),
        charge("h3", 365, -240.00, "ALLIANZ"),
        charge("h4", 547, -240.00, "ALLIANZ"),
    ]
    runs = find_recurring(charges)
    assert len(runs) == 1
    assert runs[0].period == "half-yearly"
    assert runs[0].occurrences == 4


def test_the_shortest_period_that_fits_is_the_one_reported():
    """
    A candidate stops at the first period that fits, so the periods have to be
    tried shortest first: every monthly run is also two months from every
    other charge, and a monthly subscription reported as quarterly would halve
    the total it contributes.
    """
    runs = find_recurring(monthly("SPOTIFY AB", -4.99, 8))
    assert runs[0].period == "monthly"


def test_income_can_recur_too():
    """Rent, salary and maintenance are the clearest schedules in the data."""
    runs = find_recurring(monthly("EMPLOYER", 2400.00, 5))
    assert len(runs) == 1
    assert runs[0].median_amount == pytest.approx(2400.00)


def test_two_standing_orders_from_one_payer_stay_separate():
    """
    A payer sending 200 and 300 a month is running two schedules, and neither
    may swallow the other. This is real: an employer in the live data pays two
    different regular amounts.
    """
    charges = monthly("PAYER", 200.00, 5, start_id=0)
    # Offset by a fortnight, as two standing orders normally are.
    charges += [
        charge(f"PAYER-{100 + i}", round(i * 30.4) + 14, 300.00, "PAYER")
        for i in range(5)
    ]
    runs = find_recurring(charges)
    assert len(runs) == 2
    assert {round(run.median_amount) for run in runs} == {200, 300}


def test_two_schedules_on_the_very_same_dates_are_still_separate():
    """
    The hardest version of the same case: two standing orders that bill on
    identical dates. Coverage is measured against everything the merchant
    charged, so without discounting the other schedule each would halve the
    other's score and neither would be found.
    """
    charges = monthly("PAYER", 200.00, 5, start_id=0)
    charges += monthly("PAYER", 300.00, 5, start_id=100)
    runs = find_recurring(charges)
    assert len(runs) == 2
    assert {round(run.median_amount) for run in runs} == {200, 300}


# --- What must not be found --------------------------------------------------


def test_one_missed_month_does_not_end_a_subscription():
    """
    A direct debit that fails once, or a month billed at an odd amount, leaves
    a hole in the run. Before the run could bridge it, everything after the
    hole was thrown away and the schedule was reported as having stopped on
    whatever date the run happened to break — while the merchant was visibly
    still charging every month.
    """
    charges = monthly("VERMIETER", -399.0, 8)
    del charges[5]

    runs = find_recurring(charges)
    assert len(runs) == 1
    assert runs[0].occurrences == 7
    # The run reaches the merchant's most recent charge, which is the whole
    # point: liveness is judged from that date.
    assert max(c.date for c in charges) == max(
        c.date for c in charges if str(c.id) in runs[0].transaction_ids
    )


def test_a_skipped_period_cannot_be_what_qualifies_a_run():
    """
    A skip is an assumption that a charge should have been there, not an
    observation that it was. Three drugstore visits with a month missing
    between two of them are a coincidence; letting the skip make up the
    numbers turned several of those into reported subscriptions.
    """
    charges = [
        charge("r1", 0, -12.40, "ROSSMANN"),
        charge("r2", 23, -13.10, "ROSSMANN"),
        charge("r3", 80, -12.90, "ROSSMANN"),
    ]
    assert find_recurring(charges) == []


def test_two_missed_months_are_a_different_schedule():
    """One absence is a hiccup; a pattern held together by absences is not."""
    charges = monthly("VERMIETER", -399.0, 9)
    del charges[6]
    del charges[3]

    runs = find_recurring(charges)
    # The longer of the two stretches survives, and it stops at the second
    # hole rather than reaching across it.
    assert len(runs) == 1
    assert runs[0].occurrences < 7


def test_a_half_yearly_run_never_bridges_a_period():
    """
    Two quarters is a half-year and two half-years is a year, so a bridged gap
    on a longer period cannot be told apart from a different schedule. A
    half-yearly premium reported as quarterly would double what it costs.
    """
    charges = [
        charge("h1", 0, -240.00, "ALLIANZ"),
        charge("h2", 91, -240.00, "ALLIANZ"),
        charge("h3", 182, -240.00, "ALLIANZ"),
        charge("h4", 273, -240.00, "ALLIANZ"),
    ]
    runs = find_recurring(charges)
    assert runs[0].period == "quarterly", "a real quarterly run stays quarterly"

    skipped = [
        charge("s1", 0, -240.00, "HUK"),
        charge("s2", 182, -240.00, "HUK"),
        charge("s3", 364, -240.00, "HUK"),
    ]
    assert find_recurring(skipped)[0].period == "half-yearly"


def test_two_charges_are_not_a_schedule():
    assert find_recurring(monthly("SPOTIFY AB", -4.99, 2)) == []


def test_a_frequently_visited_shop_is_not_a_subscription():
    """
    The failure this detector kept producing. Somebody who shops three
    hundred times will, by chance, produce three visits about a month apart
    for a similar amount. What makes it not a subscription is everything else
    the shop charged in between.
    """
    charges = []
    for day in range(0, 180, 3):  # a visit every three days
        charges.append(charge(f"shop-{day}", day, -20.00 - (day % 7), "EDEKA"))
    runs = find_recurring(charges)
    assert runs == [], "a shop visited every few days is not a subscription"


def test_irregular_intervals_are_not_a_schedule():
    charges = [
        charge("a", 0, -30.00, "RANDOM"),
        charge("b", 4, -30.00, "RANDOM"),
        charge("c", 9, -30.00, "RANDOM"),
        charge("d", 12, -30.00, "RANDOM"),
    ]
    assert find_recurring(charges) == []


def test_an_average_that_looks_monthly_is_not_enough():
    """
    Twelve charges in a week and one a year later average out to monthly.
    Intervals are checked pair by pair for exactly this reason.
    """
    charges = [charge(f"burst-{i}", i, -10.00, "BURST") for i in range(12)]
    charges.append(charge("far", 365, -10.00, "BURST"))
    assert find_recurring(charges) == []


def test_a_line_with_no_merchant_is_ignored():
    """A truncated PayPal line has no merchant to group on."""
    charges = [
        SimpleNamespace(
            id=f"pp-{i}",
            date=START + timedelta(days=round(i * 30.4)),
            amount=-9.99,
            booking_text=(
                "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. "
                "Buchungstext: 102417739613 PP.5189.PP . , Ihr Ei nkauf bei "
                "Ref. J8222353/1"
            ),
            process="Lastschrift / Belastung",
        )
        for i in range(5)
    ]
    assert find_recurring(charges) == []


# --- Wiring ------------------------------------------------------------------


def test_transaction_ids_covers_every_run():
    charges = monthly("SPOTIFY AB", -4.99, 4) + monthly("GYM", -30.00, 4, 100)
    runs = find_recurring(charges)
    ids = recurring_transaction_ids(runs)
    assert len(ids) == 8
    assert ids == {tid for run in runs for tid in run.transaction_ids}


def test_runs_are_ordered_by_length():
    charges = monthly("SHORT", -5.00, 3) + monthly("LONG", -9.00, 7, 100)
    runs = find_recurring(charges)
    assert [run.occurrences for run in runs] == [7, 3]


def test_the_minimum_can_be_raised():
    charges = monthly("SPOTIFY AB", -4.99, 3)
    assert find_recurring(charges, min_occurrences=3)
    assert find_recurring(charges, min_occurrences=4) == []
