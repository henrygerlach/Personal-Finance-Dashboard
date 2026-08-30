"""
Tests for spending-pattern detection.

Two things are being defended here, and they pull against each other. A
detector that finds nothing is useless, and a detector that finds everything
is worse than useless — a page of thirty cards is a page nobody reads, and the
one card that mattered is in it somewhere. So roughly half of these tests
assert that something is found, and the other half assert that a plausible
near-miss is *not*.

The other recurring theme is scale independence. Every threshold in the module
is a share of the ledger's own median month, because a rule that fires above
thirty euros is a rule written for one person's bank account.
"""

import calendar
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.patterns import (
    KIND_ARRIVED,
    KIND_CASH,
    KIND_COMMITMENT,
    KIND_DRIFT,
    KIND_GONE,
    KIND_LAPSED,
    KIND_MIX,
    KIND_OUTLIER,
    KIND_PRICE,
    KIND_SAVINGS,
    KIND_SEASONAL,
    Schedule,
    analyse,
    complete_months,
)

#: Every test ledger ends here, on a month boundary, so nothing is discarded
#: as a partial month unless the test is about that.
LAST_MONTH = (2026, 7)


def month_of(offset: int) -> tuple[int, int]:
    """The year and month ``offset`` months before the end of the ledger."""
    index = LAST_MONTH[0] * 12 + LAST_MONTH[1] - 1 - offset
    return index // 12, index % 12 + 1


def spend(
    offset: int,
    amount: float,
    minor: str = "Groceries",
    major: str = "Food & Drink",
    merchant: str = "EDEKA",
    day: int = 12,
    tid: str | None = None,
):
    """One outgoing transaction, ``offset`` months before the end."""
    year, month = month_of(offset)
    day = min(day, calendar.monthrange(year, month)[1])
    return SimpleNamespace(
        id=tid or f"{merchant}-{year}{month:02d}-{day}-{amount}",
        date=datetime(year, month, day),
        amount=-abs(amount),
        booking_text=f" {merchant} ",
        process="Lastschrift / Belastung",
        major_category=major,
        minor_category=minor,
        flow_type="expense",
    )


def earned(offset: int, amount: float = 3000.0):
    year, month = month_of(offset)
    return SimpleNamespace(
        id=f"salary-{year}{month:02d}",
        date=datetime(year, month, 1),
        amount=abs(amount),
        booking_text=" ARBEITGEBER GMBH ",
        process="Gutschrift",
        major_category="Income",
        minor_category="Salary & Wages",
        flow_type="income",
    )


def ledger(months: int = 30, monthly: float = 1000.0, **kwargs):
    """
    A steady ledger: the same spending every month, in one category.

    The baseline every test perturbs. Split into four payments a month so a
    detector that needs a transaction count has one to work with, and so the
    "more often" half of a decomposition has room to move.
    """
    rows = []
    for offset in range(months):
        for index in range(4):
            rows.append(
                spend(
                    offset,
                    monthly / 4,
                    day=5 + index * 8,
                    tid=f"base-{offset}-{index}",
                    **kwargs,
                )
            )
    return rows


def find(result, kind: str) -> list:
    return [pattern for pattern in result.findings if pattern.kind == kind]


def only(result, kind: str):
    matches = find(result, kind)
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


# --- Which months may be compared at all -------------------------------------


def test_the_newest_month_is_dropped_while_its_statement_is_still_arriving():
    """
    Statements are uploaded roughly monthly, so the newest month in the
    database is usually a few days of one. Comparing it to anything reports
    the same catastrophic collapse in spending every time.
    """
    rows = ledger(months=6)
    rows.append(spend(-1, 40.0, day=3, tid="partial"))

    months = complete_months(rows)

    assert "2026-08" not in months
    assert months[-1] == "2026-07"


def test_a_month_the_history_reaches_the_end_of_is_kept():
    """The slack has to be small enough that ordinary months survive it."""
    rows = ledger(months=6)
    assert complete_months(rows)[-1] == "2026-07"


def test_the_first_month_is_dropped_when_the_history_starts_mid_month():
    """A ledger opened on the 14th has half a January in it, and half a
    January is a spending collapse that never happened."""
    rows = ledger(months=6)
    rows = [row for row in rows if row.date.day > 20 or row.date.month != 2]
    rows.append(spend(5, 100.0, day=22, tid="opening"))

    assert complete_months(rows)[0] != "2026-02"


# --- A category settling at a new level --------------------------------------


def test_a_category_that_settles_at_a_new_level_is_reported():
    rows = ledger(months=30)
    rows += [
        spend(offset, 100.0, day=20, tid=f"extra-{offset}-{i}")
        for offset in range(3)
        for i in range(2)
    ]

    result = analyse(rows)

    drift = only(result, KIND_DRIFT)
    assert drift.headline == pytest.approx(200.0)
    assert drift.facts["before"] == pytest.approx(1000.0)
    assert drift.facts["after"] == pytest.approx(1200.0)


def test_one_expensive_month_is_not_a_new_level():
    """
    The difference between this and an outlier is the whole point of having
    both. Three months are compared by median precisely so that one of them
    being a holiday does not move the answer.
    """
    rows = ledger(months=30)
    rows.append(spend(1, 600.0, day=20, tid="one-off"))

    assert not find(analyse(rows), KIND_DRIFT)


def test_a_large_relative_move_in_a_tiny_category_is_not_reported():
    """
    "Pet food up 140%" on a ledger where pet food is four euros is noise
    wearing a percentage. The floor is what keeps it off the page.
    """
    rows = ledger(months=30)
    rows += [
        spend(
            offset, 3.0, minor="Pet Food & Supplies", major="Pets", tid=f"pet-{offset}"
        )
        for offset in range(30)
    ]
    rows += [
        spend(
            offset,
            7.0,
            minor="Pet Food & Supplies",
            major="Pets",
            day=20,
            tid=f"pet-extra-{offset}",
        )
        for offset in range(3)
    ]

    assert not find(analyse(rows), KIND_DRIFT)


def test_the_same_ledger_ten_times_larger_finds_the_same_things():
    """
    Every threshold is a share of the ledger's own median month, so the module
    behaves identically for somebody spending ten times as much. A hard euro
    floor anywhere in it would break this test.
    """
    small = ledger(months=30, monthly=400.0)
    small += [spend(offset, 80.0, day=20, tid=f"s-{offset}") for offset in range(3)]

    large = ledger(months=30, monthly=4000.0)
    large += [spend(offset, 800.0, day=20, tid=f"l-{offset}") for offset in range(3)]

    assert [p.kind for p in analyse(small).findings] == [
        p.kind for p in analyse(large).findings
    ]


def test_a_move_is_split_into_going_more_often_and_paying_more_each_time():
    """
    The question a bar chart cannot answer. Eleven visits at the old price and
    four visits at a higher one are the same extra euros and completely
    different facts, so the card has to be able to say which happened.
    """
    rows = ledger(months=30)
    rows += [
        spend(offset, 250.0, day=20 + index, tid=f"more-{offset}-{index}")
        for offset in range(3)
        for index in range(2)
    ]

    drift = only(analyse(rows), KIND_DRIFT)

    assert drift.facts["count_before"] == 4
    assert drift.facts["count_after"] == 6
    assert drift.facts["from_frequency"] == pytest.approx(500.0)
    assert drift.facts["from_ticket"] == pytest.approx(0.0)


def test_the_two_halves_of_a_split_add_up_to_the_whole_move():
    """
    The decomposition is exact rather than approximate, and a card that
    reported two halves not summing to the headline would be worse than one
    that reported neither.
    """
    rows = ledger(months=30)
    rows += [spend(offset, 300.0, day=20, tid=f"a-{offset}") for offset in range(3)]

    drift = only(analyse(rows), KIND_DRIFT)

    assert drift.facts["from_frequency"] + drift.facts["from_ticket"] == pytest.approx(
        drift.headline, abs=0.01
    )


def test_the_minor_category_that_explains_a_major_move_is_what_gets_reported():
    """
    "Food & Drink is up 180" next to "Delivery & Takeout is up 165" is the
    same fact twice, and only the second one has the answer in it.
    """
    rows = ledger(months=30)
    rows += [
        spend(
            offset,
            150.0,
            minor="Delivery & Takeout",
            day=20,
            merchant="LIEFERANDO",
            tid=f"food-{offset}",
        )
        for offset in range(3)
    ]

    drift = only(analyse(rows), KIND_DRIFT)

    assert drift.subject == "Delivery & Takeout"
    assert drift.facts["major"] == "Food & Drink"


def test_a_major_that_drifted_in_several_places_at_once_stays_a_major():
    """No single minor explains it, so naming one would be picking a
    favourite out of three equal causes."""
    rows = ledger(months=30)
    for index, minor in enumerate(
        ["Restaurants & Bars", "Cafés & Snacks", "Delivery & Takeout"]
    ):
        rows += [
            spend(
                offset,
                90.0,
                minor=minor,
                day=18 + index,
                merchant=f"M{index}",
                tid=f"{minor}-{offset}",
            )
            for offset in range(3)
        ]

    drift = only(analyse(rows), KIND_DRIFT)

    assert drift.subject == "Food & Drink"


# --- Months unlike the others ------------------------------------------------


def test_an_extraordinary_month_is_found():
    rows = ledger(months=30)
    rows.append(
        spend(
            4,
            3000.0,
            minor="Furniture & Household Goods",
            major="Housing",
            merchant="IKEA",
            tid="sofa",
        )
    )

    outlier = only(analyse(rows), KIND_OUTLIER)

    assert outlier.month == "2026-03"
    assert outlier.facts["spend"] == pytest.approx(4000.0)


def test_two_holidays_do_not_hide_each_other():
    """
    The reason spread is a median absolute deviation and not a standard
    deviation. Two extraordinary months in a five-year history inflate a
    standard deviation enough that neither can ever be found again.
    """
    rows = ledger(months=40)
    rows.append(spend(4, 3000.0, merchant="IKEA", tid="one"))
    rows.append(spend(20, 3000.0, merchant="IKEA", tid="two"))

    assert len(find(analyse(rows), KIND_OUTLIER)) == 2


def test_an_unusually_cheap_month_is_reported_too():
    """Usually the more surprising direction, and never visible in a total."""
    rows = [row for row in ledger(months=30) if not row.id.startswith("base-4-")]

    outlier = only(analyse(rows), KIND_OUTLIER)

    assert outlier.headline < 0


def test_a_month_containing_a_holiday_is_attributed_to_the_trip():
    """
    A month with a holiday in it is not an anomaly, it is a holiday — and the
    app already knows which transactions the user said belonged to it.
    """
    rows = ledger(months=30)
    rows.append(
        spend(
            4,
            3000.0,
            major="Travel",
            minor="Accommodation",
            merchant="HOTEL",
            tid="hotel",
        )
    )

    result = analyse(rows, trip_of={"hotel": "Lisbon"})

    assert only(result, KIND_OUTLIER).facts["attribution"] == {
        "reason": "trip",
        "label": "Lisbon",
        "amount": 3000.0,
    }


def test_a_month_driven_by_one_payment_names_the_merchant():
    """
    The merchant, not the category it was filed under: "one payment to a bike
    shop" is an answer and "Sports & Outdoor Gear" is the question again.
    """
    rows = ledger(months=30)
    rows.append(
        spend(
            4,
            3000.0,
            minor="Sports & Outdoor Gear",
            major="Shopping",
            merchant="BIKE DISCOUNT GMBH",
            tid="bike",
        )
    )

    attribution = only(analyse(rows), KIND_OUTLIER).facts["attribution"]

    assert attribution["reason"] == "payment"
    assert "BIKE" in attribution["label"]


def test_a_month_that_was_expensive_across_the_board_names_nothing():
    """Naming the largest of many causes would read as naming the cause."""
    rows = ledger(months=30)
    rows += [
        spend(
            4,
            250.0,
            minor=minor,
            major="Shopping",
            day=20 + index,
            merchant=f"SHOP{index}",
            tid=f"spread-{index}",
        )
        for index, minor in enumerate(
            [
                "Clothing & Shoes",
                "Electronics & Tech",
                "Home & Garden",
                "General Merchandise",
                "Sports & Outdoor Gear",
                "Other Shopping",
            ]
        )
    ]

    assert only(analyse(rows), KIND_OUTLIER).facts["attribution"] is None


# --- Standing commitments ----------------------------------------------------


def schedule(
    merchant: str = "NETFLIX",
    amount: float = -12.99,
    period: str = "monthly",
    status: str = "active",
    first: int = 24,
    last: int = 0,
    minor: str = "Streaming & Media",
    price_change: dict | None = None,
    ids: tuple = ("charge-1",),
):
    """One standing commitment, as the recurring page hands it over."""
    first_year, first_month = month_of(first)
    last_year, last_month = month_of(last)
    return Schedule(
        merchant=merchant,
        merchant_key=merchant.replace(" ", ""),
        period=period,
        minor=minor,
        amount=amount,
        direction="out",
        status=status,
        first_seen=datetime(first_year, first_month, 4),
        last_seen=datetime(last_year, last_month, 4),
        next_expected=datetime(last_year, last_month, 4) + timedelta(days=30),
        occurrences=12,
        transaction_ids=ids,
        price_change=price_change,
    )


def test_a_subscription_price_rise_is_reported_at_its_monthly_cost():
    rows = ledger(months=30)
    changed = "%04d-%02d" % month_of(5)

    result = analyse(
        rows,
        schedules=[
            schedule(price_change={"from": -12.99, "to": -17.99, "changed_on": changed})
        ],
    )

    price = only(result, KIND_PRICE)
    assert price.headline == pytest.approx(5.0)
    assert price.facts["annual"] == pytest.approx(60.0)


def test_a_yearly_premium_is_reported_at_a_twelfth_of_its_rise():
    """
    The only form in which a 240 EUR insurance and a 5 EUR subscription can be
    compared at all, and therefore the only form in which the page can rank
    them against each other.
    """
    changed = "%04d-%02d" % month_of(5)

    result = analyse(
        ledger(months=30),
        schedules=[
            schedule(
                merchant="VERSICHERUNG",
                period="yearly",
                amount=-360.0,
                minor="Insurance",
                price_change={"from": -240.0, "to": -360.0, "changed_on": changed},
            )
        ],
    )

    assert only(result, KIND_PRICE).headline == pytest.approx(10.0)


def test_a_schedule_that_stopped_collecting_is_reported():
    """Either it was cancelled and the money is free, or a collection failed
    and it is about to be taken twice. Both are worth knowing."""
    result = analyse(
        ledger(months=30),
        schedules=[schedule(merchant="FITNESS", amount=-49.0, status="ended", last=6)],
    )

    lapsed = only(result, KIND_LAPSED)
    assert lapsed.headline == pytest.approx(-49.0)
    assert lapsed.label == "FITNESS"


def test_a_schedule_still_running_is_not_reported_as_lapsed():
    result = analyse(ledger(months=30), schedules=[schedule(amount=-49.0)])

    assert not find(result, KIND_LAPSED)


def test_a_negligible_schedule_ending_is_not_worth_a_card():
    """The floor applies here too: a two-euro standing order stopping is not
    news on a thousand-euro month."""
    result = analyse(
        ledger(months=30),
        schedules=[schedule(merchant="TINY", amount=-2.0, status="ended", last=6)],
    )

    assert not find(result, KIND_LAPSED)


def test_the_committed_floor_is_reported_when_it_moves():
    """
    The one number on the page that is about the future: everything in it will
    be charged again next month without anybody deciding anything.
    """
    result = analyse(
        ledger(months=30),
        schedules=[
            schedule(merchant="RENT", amount=-800.0, minor="Rent", first=24),
            schedule(merchant="GYM", amount=-60.0, minor="Fitness & Sports", first=6),
        ],
    )

    commitment = only(result, KIND_COMMITMENT)
    assert commitment.headline == pytest.approx(60.0)
    assert commitment.facts["now"] == pytest.approx(860.0)


def test_a_schedule_is_carried_at_the_price_it_charged_at_the_time():
    """
    Carrying today's price backwards would draw a commitment floor that rose
    on the day the history was re-read rather than on the day the letter
    arrived.
    """
    changed = "%04d-%02d" % month_of(3)

    result = analyse(
        ledger(months=30),
        schedules=[
            schedule(
                merchant="RENT",
                amount=-900.0,
                minor="Rent",
                first=24,
                price_change={"from": -800.0, "to": -900.0, "changed_on": changed},
            )
        ],
    )

    floor = dict(only(result, KIND_COMMITMENT).series)
    assert floor["%04d-%02d" % month_of(6)] == pytest.approx(800.0)
    assert floor["%04d-%02d" % month_of(0)] == pytest.approx(900.0)


def test_a_cancelled_schedule_is_not_also_reported_as_a_category_falling():
    """
    The same euros said twice, and only one of the two says which merchant and
    which month. A cancelled rent is a hole in Housing by arithmetic, and
    "Housing is down 800" adds nothing to "this standing order stopped".
    """
    rows = ledger(months=30)
    rows += [
        spend(
            offset,
            800.0,
            minor="Rent",
            major="Housing",
            merchant="LANDLORD",
            day=3,
            tid=f"rent-{offset}",
        )
        for offset in range(4, 30)
    ]

    result = analyse(
        rows,
        schedules=[
            schedule(
                merchant="LANDLORD",
                amount=-800.0,
                minor="Rent",
                status="ended",
                first=29,
                last=4,
                ids=tuple(f"rent-{offset}" for offset in range(4, 30)),
            )
        ],
    )

    assert find(result, KIND_LAPSED)
    assert not [p for p in find(result, KIND_DRIFT) if p.subject in ("Rent", "Housing")]


# --- Seasons -----------------------------------------------------------------


def seasonal_ledger(number: int, extra: float, years: int = 4):
    """A steady ledger with one calendar month reliably heavier."""
    rows = ledger(months=years * 12)
    for offset in range(years * 12):
        year, month = month_of(offset)
        if month == number:
            rows.append(
                spend(
                    offset,
                    extra,
                    minor="Gifts",
                    major="Gifts & Donations",
                    merchant="GIFTSHOP",
                    day=10,
                    tid=f"season-{offset}",
                )
            )
    return rows


def test_a_calendar_month_that_is_reliably_heavier_is_reported():
    """August is one month away from a ledger ending in July."""
    result = analyse(seasonal_ledger(8, 400.0))

    season = only(result, KIND_SEASONAL)
    assert season.facts["calendar_month"] == 8
    assert season.facts["months_away"] == 1
    assert season.label == "Gifts & Donations"


def test_a_season_too_far_ahead_to_act_on_is_left_to_the_profile_chart():
    """
    A five-year history has a seasonal fact about every month of the year for
    every category. Put them all on the page and the two that could change a
    decision are buried in ten that could not.
    """
    assert not find(analyse(seasonal_ledger(1, 400.0)), KIND_SEASONAL)


def test_two_memorable_years_are_not_a_season():
    """
    A median puts half the observations on the high side by construction, so
    without a consistency test two heavy Augusts and three ordinary ones read
    exactly like a season.
    """
    rows = ledger(months=48)
    for offset in range(48):
        year, month = month_of(offset)
        if month == 8 and year in (2023, 2024):
            rows.append(
                spend(
                    offset,
                    900.0,
                    minor="Gifts",
                    major="Gifts & Donations",
                    merchant="GIFTSHOP",
                    day=10,
                    tid=f"odd-{offset}",
                )
            )

    assert not find(analyse(rows), KIND_SEASONAL)


def test_a_season_needs_more_than_two_years_to_be_one():
    assert not find(analyse(seasonal_ledger(8, 400.0, years=2)), KIND_SEASONAL)


# --- Merchants coming and going ----------------------------------------------


def test_a_regular_merchant_that_went_quiet_is_reported():
    rows = ledger(months=30)
    rows += [
        spend(
            offset,
            60.0,
            minor="Restaurants & Bars",
            merchant="TRATTORIA",
            day=17,
            tid=f"trat-{offset}",
        )
        for offset in range(8, 30)
    ]

    gone = only(analyse(rows), KIND_GONE)

    assert gone.label == "TRATTORIA"
    assert gone.facts["visits"] == 22


def test_silence_is_measured_against_the_merchants_own_rhythm():
    """
    A supermarket visited twice a week is gone after a month; a barber is not
    gone after two. A fixed number of weeks reports one of them wrongly
    whichever number is chosen.
    """
    rows = ledger(months=30)
    rows += [
        spend(
            offset * 4,
            90.0,
            minor="Personal Care & Beauty",
            major="Health & Personal Care",
            merchant="BARBER",
            day=9,
            tid=f"barber-{offset}",
        )
        for offset in range(1, 8)
    ]

    assert not find(analyse(rows), KIND_GONE)


def test_a_merchant_on_a_schedule_is_left_to_the_recurring_page():
    """
    Excluding only the scheduled charges is not enough. A university billing a
    monthly fee also sells the odd coffee, and those leftovers went quiet on
    exactly the same day — which reported one cancellation twice, with two
    different amounts.
    """
    rows = ledger(months=30)
    rows += [
        spend(
            offset,
            200.0,
            minor="Tuition & Courses",
            major="Family & Education",
            merchant="UNI HAMBURG",
            day=6,
            tid=f"uni-{offset}",
        )
        for offset in range(8, 30)
    ]
    rows += [
        spend(
            offset,
            30.0,
            minor="Cafés & Snacks",
            major="Food & Drink",
            merchant="UNI HAMBURG",
            day=21,
            tid=f"unicafe-{offset}",
        )
        for offset in range(8, 30)
    ]

    result = analyse(
        rows,
        schedules=[
            schedule(
                merchant="UNI HAMBURG",
                amount=-200.0,
                minor="Tuition & Courses",
                status="ended",
                first=29,
                last=8,
                ids=tuple(f"uni-{offset}" for offset in range(8, 30)),
            )
        ],
    )

    assert not find(result, KIND_GONE)


def test_a_new_merchant_that_became_a_habit_is_reported():
    rows = ledger(months=30)
    rows += [
        spend(
            offset,
            120.0,
            minor="Fitness & Sports",
            major="Health & Personal Care",
            merchant="CLIMBING GYM",
            day=14,
            tid=f"gym-{offset}",
        )
        for offset in range(4)
    ]

    arrived = only(analyse(rows), KIND_ARRIVED)

    assert arrived.label == "CLIMBING GYM"
    assert arrived.facts["visits"] == 4


def test_a_single_purchase_from_a_new_merchant_is_not_a_habit():
    rows = ledger(months=30)
    rows.append(
        spend(
            1,
            400.0,
            minor="Electronics & Tech",
            major="Shopping",
            merchant="MEDIAMARKT",
            day=14,
            tid="tv",
        )
    )

    assert not find(analyse(rows), KIND_ARRIVED)


# --- Composition, income and cash --------------------------------------------


def test_a_substitution_that_leaves_the_total_untouched_is_reported():
    """
    Invisible in every total on the dashboard: eating in less and eating out
    more is one bar up, one bar down, and a monthly figure that never moves.
    """
    rows = ledger(months=30)
    swapped = []
    for row in rows:
        if row.id.startswith(("base-0-", "base-1-", "base-2-")) and row.id.endswith(
            ("-0", "-1")
        ):
            swapped.append(
                spend(
                    int(row.id.split("-")[1]),
                    250.0,
                    minor="Events & Culture",
                    major="Leisure & Entertainment",
                    merchant="KONZERTKASSE",
                    day=row.date.day,
                    tid=f"swap-{row.id}",
                )
            )
        else:
            swapped.append(row)

    mix = only(analyse(swapped), KIND_MIX)

    assert mix.facts["up"] == "Leisure & Entertainment"
    assert mix.facts["down"] == "Food & Drink"


def test_the_share_of_income_kept_is_reported_when_it_moves():
    """
    The one measure here that cannot be read off a spending chart: spending
    can fall while the share kept falls too, if income fell faster.
    """
    rows = ledger(months=30)
    rows += [earned(offset, 3000.0) for offset in range(3, 30)]
    rows += [earned(offset, 1200.0) for offset in range(3)]

    savings = only(analyse(rows), KIND_SAVINGS)

    assert savings.facts["before"] > savings.facts["after"]


def test_a_month_without_income_is_left_out_rather_than_counted_as_zero():
    """
    On a ledger where the salary occasionally lands a day early, a month
    without one is a calendar artefact and not a month of infinite spending.
    """
    rows = ledger(months=30)
    rows += [earned(offset, 3000.0) for offset in range(30) if offset != 1]

    assert not find(analyse(rows), KIND_SAVINGS)


def test_cash_withdrawals_are_watched_even_though_they_are_not_spending():
    """
    A withdrawal is a transfer and stays out of every expense total, which is
    right — what it turns into never reaches the bank. But a ledger where it
    moves is one where the categorised spending covers less than it used to.
    """
    rows = ledger(months=30)
    rows += [
        SimpleNamespace(
            id=f"atm-{offset}",
            date=datetime(*month_of(offset), 11),
            amount=-200.0 if offset < 3 else -50.0,
            booking_text=" GELDAUTOMAT ",
            process="Auszahlung",
            major_category="Transfers & Savings",
            minor_category="Cash Withdrawal",
            flow_type="transfer",
        )
        for offset in range(30)
    ]

    cash = only(analyse(rows), KIND_CASH)

    assert cash.headline == pytest.approx(150.0)


def test_cash_withdrawals_stay_out_of_the_spending_series():
    """Whatever the card says about them, they are not spending: counting
    them would double every euro that was later spent by card."""
    rows = ledger(months=30)
    rows += [
        SimpleNamespace(
            id=f"atm-{offset}",
            date=datetime(*month_of(offset), 11),
            amount=-200.0,
            booking_text=" GELDAUTOMAT ",
            process="Auszahlung",
            major_category="Transfers & Savings",
            minor_category="Cash Withdrawal",
            flow_type="transfer",
        )
        for offset in range(30)
    ]

    assert analyse(rows).spend[-1] == pytest.approx(1000.0)


# --- The page's order --------------------------------------------------------


def test_findings_are_ranked_by_what_they_are_worth_and_not_by_date():
    """
    A page sorted by recency buries the rent under a coffee shop. The newest
    finding is not the largest, and euros a month is the only ordering that
    puts the eye where the money is.
    """
    rows = ledger(months=30)
    rows += [spend(offset, 400.0, day=20, tid=f"big-{offset}") for offset in range(3)]
    rows.append(spend(10, 4000.0, merchant="IKEA", tid="sofa"))

    result = analyse(rows)

    assert [abs(p.headline) for p in result.findings] == sorted(
        (abs(p.headline) for p in result.findings), reverse=True
    )
    assert result.findings[0].kind == KIND_OUTLIER


def test_an_empty_ledger_produces_nothing_rather_than_failing():
    """The page is reachable before the first upload."""
    result = analyse([])

    assert result.findings == ()
    assert result.months == ()
