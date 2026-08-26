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
