"""
Tests for trips: what one journey cost.

The point the whole feature turns on is that a trip's spending is not
contained in its dates. So the tests that matter most are the ones about the
edges — a flight bought months early, a card payment that settles after the
flight home — and about the two things a naive date filter gets wrong in the
middle: the rent that was debited while the flat stood empty, and the cash
withdrawal that is the only record of a week's spending.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.trips import (
    BOOKING_LEAD_DAYS,
    BOOKING_TICK_DAYS,
    PHASE_BOOKED,
    PHASE_DURING,
    PHASE_SETTLED,
    SAME_DAY_CHARGES,
    SETTLEMENT_LAG_DAYS,
    SETTLEMENT_TAIL_DAYS,
    STRENGTH_LIKELY,
    STRENGTH_STRONG,
    STRENGTH_WEAK,
    assigned_phase,
    counts_as_spending,
    find_candidates,
    find_trip_candidates,
    home_country,
    is_experience_platform,
    overlaps,
    phase_for,
    summarize,
)

START = datetime(2026, 7, 4)
END = datetime(2026, 7, 18)


def tx(
    tid="1",
    amount=-42.0,
    date=None,
    major="Food & Drink",
    minor="Restaurants & Bars",
    **extra,
):
    defaults = dict(
        id=tid,
        date=date or datetime(2026, 7, 10),
        amount=amount,
        booking_text=" CERVEJARIA RAMIRO LISBOA ",
        process=None,
        major_category=major,
        minor_category=minor,
        flow_type="expense",
        is_recurring=False,
        paypal_counterparty=None,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def flight(tid="f", date=datetime(2026, 3, 2), amount=-380.0):
    return tx(
        tid,
        amount=amount,
        date=date,
        major="Travel",
        minor="Flights & Long-Distance",
        booking_text=" TAP AIR PORTUGAL ",
    )


def ids(candidates):
    return [candidate.transaction_id for candidate in candidates]


def by_id(candidates):
    return {candidate.transaction_id: candidate for candidate in candidates}


# --- The three windows -------------------------------------------------------


def test_the_trips_own_days_are_inclusive_at_both_ends():
    """
    A dinner on the last evening is the trip, not the settlement tail. Off by
    one here would move every final-day charge into a different section of the
    page and out of "while away" in the phase breakdown.
    """
    assert phase_for(START, START, END) == PHASE_DURING
    assert phase_for(END, START, END) == PHASE_DURING
    assert phase_for(END + timedelta(hours=23), START, END) == PHASE_DURING
    assert phase_for(START - timedelta(days=1), START, END) == PHASE_BOOKED
    assert phase_for(END + timedelta(days=1), START, END) == PHASE_SETTLED


def test_the_suggester_stops_at_its_horizons_but_assignment_does_not():
    """
    The suggester has to be bounded or it would propose the whole ledger. A
    decision made by hand must not be: a deposit paid two years out is still a
    booking, and a friend settling up in the autumn is still a late settlement.
    """
    long_before = START - timedelta(days=BOOKING_LEAD_DAYS + 1)
    long_after = END + timedelta(days=SETTLEMENT_TAIL_DAYS + 2)

    assert phase_for(long_before, START, END) is None
    assert phase_for(long_after, START, END) is None

    assert assigned_phase(long_before, START, END) == PHASE_BOOKED
    assert assigned_phase(long_after, START, END) == PHASE_SETTLED


def test_a_flight_bought_months_early_is_proposed():
    """The case the whole feature exists for."""
    candidates = by_id(find_candidates([flight()], START, END))
    assert candidates["f"].phase == PHASE_BOOKED
    assert candidates["f"].suggested
    assert "before departure" in candidates["f"].reason


def test_only_travel_is_proposed_from_the_lead_in():
    """
    A year of everything bought before a holiday would bury the four lines the
    window exists to find. Anything else is reached through the search box.
    """
    groceries = tx("g", date=START - timedelta(days=30), minor="Groceries")
    assert ids(find_candidates([groceries, flight()], START, END)) == ["f"]


# --- What a date filter gets wrong in the middle -----------------------------


def test_a_payment_that_repeats_on_a_schedule_is_not_the_trip():
    """
    The rent was debited whether or not the flat stood empty. Detection has
    already worked out which payments repeat, so the trip suggester does not
    have to guess.
    """
    rent = tx(
        "r",
        amount=-820.0,
        date=datetime(2026, 7, 6),
        major="Housing",
        minor="Rent",
        is_recurring=True,
    )
    candidate = by_id(find_candidates([rent], START, END))["r"]
    assert not candidate.suggested
    assert candidate.strength == STRENGTH_WEAK


def test_a_transfer_during_the_trip_is_not_spending():
    """A savings plan that ran mid-holiday is not what the holiday cost."""
    savings = tx(
        "s",
        amount=-300.0,
        major="Transfers & Savings",
        minor="Savings Contribution",
        flow_type="transfer",
    )
    assert not by_id(find_candidates([savings], START, END))["s"].suggested
    assert not counts_as_spending(savings)


def test_cash_taken_out_on_the_trip_is_the_trips_spending():
    """
    The deliberate exception to the transfer rule. What the cash paid for
    never reaches the bank, so the withdrawal is the only record there will
    ever be of it — and the dashboard's own expense totals leave it out.
    """
    cash = tx(
        "c",
        amount=-200.0,
        major="Transfers & Savings",
        minor="Cash Withdrawal",
        flow_type="transfer",
    )
    candidate = by_id(find_candidates([cash], START, END))["c"]
    assert candidate.suggested
    assert candidate.strength == STRENGTH_STRONG
    assert counts_as_spending(cash)

    summary = summarize([(cash, 1.0)], START, END)
    assert summary["spend"] == 200.0
    assert summary["cash"] == 200.0


def test_cash_taken_out_after_the_return_is_next_weeks_cash():
    """Nothing connects it to the trip, and it is a large number to get wrong."""
    cash = tx(
        "c",
        amount=-200.0,
        date=END + timedelta(days=3),
        major="Transfers & Savings",
        minor="Cash Withdrawal",
        flow_type="transfer",
    )
    assert find_candidates([cash], START, END) == []


# --- The settlement tail -----------------------------------------------------


def test_a_merchant_seen_on_the_trip_settling_late_is_the_strong_signal():
    """
    The bank books a card payment days after it is made, so the same shop
    appearing a week after the flight home is that visit reaching the account
    — not a second visit.
    """
    during = tx("a", date=datetime(2026, 7, 12))
    late = tx("b", date=END + timedelta(days=5))
    candidates = by_id(find_candidates([during, late], START, END))
    assert candidates["b"].phase == PHASE_SETTLED
    assert candidates["b"].strength == STRENGTH_STRONG
    assert candidates["b"].suggested


def test_an_unconnected_charge_in_the_tail_is_offered_but_not_ticked():
    """
    The three weeks after a holiday are ordinary life as much as they are a
    settlement window. Hiding those lines would lose a genuine late booking;
    ticking them would inflate every trip.
    """
    stranger = tx("z", date=END + timedelta(days=4), booking_text=" EDEKA BOECKER ")
    candidate = by_id(find_candidates([stranger], START, END))["z"]
    assert not candidate.suggested
    assert candidate.strength == STRENGTH_WEAK


def test_the_merchants_of_the_trip_are_collected_before_the_tail_is_read():
    """
    A late settlement is recognised by a merchant seen during the trip, so the
    set has to be complete before any tail row is judged — otherwise the
    answer would depend on the order the history happened to arrive in.
    """
    late = tx("b", date=END + timedelta(days=2))
    during = tx("a", date=datetime(2026, 7, 12))
    # Tail row first: if the merchants were gathered as the walk went, this
    # would come out unticked.
    candidates = by_id(find_candidates([late, during], START, END))
    assert candidates["b"].suggested


# --- What the real data taught -----------------------------------------------
#
# Both rules below were written the other way round first, run against four
# years of real statements, and found to be badly wrong. The tests are the
# record of what the wrong version did.


def test_the_supermarket_at_home_is_not_a_late_settlement():
    """
    Matching a tail charge on "a merchant seen during the trip" alone ticked a
    fortnight of ordinary shopping as holiday spending: the supermarket at the
    end of the street is seen during the trip too, on the morning before the
    flight. A merchant appearing 213 times across the history says nothing
    about where anybody was.
    """
    history = [
        # Years of shopping at home, including the morning the trip began.
        tx(
            f"h{i}",
            date=datetime(2026, 1, 1) + timedelta(days=i * 30),
            booking_text=" EDEKA BOECKER HAMBURG ",
        )
        for i in range(6)
    ]
    at_home_during = tx("d", date=START, booking_text=" EDEKA BOECKER HAMBURG ")
    after = tx(
        "a", date=END + timedelta(days=3), booking_text=" EDEKA BOECKER HAMBURG "
    )

    candidate = by_id(find_candidates(history + [at_home_during, after], START, END))[
        "a"
    ]
    assert not candidate.suggested
    assert candidate.strength == STRENGTH_WEAK


def test_a_merchant_seen_only_on_the_trip_settling_late_is_ticked():
    """The case the tail exists for, and the one the fix must not break."""
    on_trip = tx("d", date=datetime(2026, 7, 12), booking_text=" GLASKOGEN.SE ARVIKA ")
    late = tx("a", date=END + timedelta(days=3), booking_text=" GLASKOGEN.SE ARVIKA ")
    noise = [
        tx(
            f"n{i}",
            date=datetime(2026, 2, i + 1),
            booking_text=" EDEKA BOECKER HAMBURG ",
        )
        for i in range(5)
    ]

    candidate = by_id(find_candidates(noise + [on_trip, late], START, END))["a"]
    assert candidate.suggested
    assert candidate.strength == STRENGTH_STRONG


def test_a_hotel_that_bills_after_checkout_is_not_disqualified_by_its_own_bill():
    """
    The merchant set asks whether a merchant occurs outside the trip, and the
    tail must not count as outside — a late bill is exactly what is being
    looked for, and letting it disqualify itself would defeat the window.
    """
    stay = tx("d", date=datetime(2026, 7, 8), booking_text=" HOTEL SOLAR DO CASTELO ")
    bill = tx(
        "b", date=END + timedelta(days=2), booking_text=" HOTEL SOLAR DO CASTELO "
    )
    assert by_id(find_candidates([stay, bill], START, END))["b"].suggested


def test_nothing_is_ticked_on_timing_alone_once_the_settlement_lag_has_passed():
    """
    The bank books a card payment within about a week. Two weeks later the
    window is ordinary life resuming, so a match there is a coincidence.
    """
    on_trip = tx("d", date=datetime(2026, 7, 12), booking_text=" GLASKOGEN.SE ARVIKA ")
    much_later = tx(
        "l",
        date=END + timedelta(days=SETTLEMENT_LAG_DAYS + 4),
        booking_text=" GLASKOGEN.SE ARVIKA ",
    )
    assert not by_id(find_candidates([on_trip, much_later], START, END))["l"].suggested


def test_last_years_holiday_is_not_ticked_as_this_years_booking():
    """
    Travel categories hold two different things: a booking made from home and
    a fortnight paid for on the ground. Proposing every travel charge in the
    lead-in ticked twenty-five lines of a previous trip on the real data —
    bus fares and homestays from a holiday ten months earlier.
    """
    bus_fare = flight("b", date=START - timedelta(days=300), amount=-2.73)
    homestay = tx(
        "s",
        amount=-23.43,
        date=START - timedelta(days=305),
        major="Travel",
        minor="Accommodation",
    )
    real_booking = flight("r", date=START - timedelta(days=33), amount=-1189.60)

    candidates = by_id(find_candidates([bus_fare, homestay, real_booking], START, END))
    # Still listed — hiding an unusual booking would defeat the window — but
    # not ticked, because a ticked wrong answer costs more than an unticked
    # right one.
    assert not candidates["b"].suggested
    assert not candidates["s"].suggested
    assert candidates["r"].suggested


def test_a_large_booking_beyond_the_ticking_horizon_is_listed_not_ticked():
    """At a year out the window is mostly the previous holiday."""
    early = flight(
        "e", date=START - timedelta(days=BOOKING_TICK_DAYS + 10), amount=-900.0
    )
    assert not by_id(find_candidates([early], START, END))["e"].suggested


def test_on_the_ground_travel_spending_is_not_proposed_from_the_lead_in():
    """
    "Other Travel" is where charges made on a trip land, so it is the one
    travel category the booking window must not read.
    """
    on_the_ground = tx(
        "o",
        amount=-24.06,
        date=START - timedelta(days=75),
        major="Travel",
        minor="Other Travel",
    )
    assert find_candidates([on_the_ground], START, END) == []


# --- Totals ------------------------------------------------------------------


def test_money_that_came_back_is_reported_next_to_the_gross_not_netted_away():
    """
    A friend's half of the house is not negative spending. The gross is what
    the trip cost and the net is what it cost you, and a trip where both are
    large is a different trip from one where both are small.
    """
    house = tx("h", amount=-1200.0, major="Travel", minor="Accommodation")
    payback = tx(
        "p",
        amount=600.0,
        date=END + timedelta(days=40),
        major="Transfers & Savings",
        minor="Person to Person",
        flow_type="transfer",
    )
    # A repayment filed as a person-to-person transfer is still money back, so
    # it is assigned by hand; the total has to see it.
    refund = tx(
        "f",
        amount=180.0,
        major="Income",
        minor="Refunds & Reimbursements",
        flow_type="income",
    )

    summary = summarize([(house, 1.0), (payback, 1.0), (refund, 1.0)], START, END)
    assert summary["spend"] == 1200.0
    # The friend's half counts as money back rather than as negative spending,
    # so the gross still says what the house cost.
    assert summary["refunds"] == 780.0
    assert summary["net"] == 420.0
    assert summary["excluded"] == 0.0


def test_a_share_splits_one_booking_between_who_it_covers():
    """Two flights on one card, one of them somebody else's."""
    booking = tx("b", amount=-760.0, major="Travel", minor="Flights & Long-Distance")
    summary = summarize([(booking, 0.5)], START, END)
    assert summary["spend"] == 380.0
    assert summary["categories"][0]["amount"] == 380.0


def test_the_phase_split_shows_what_a_date_filter_would_have_missed():
    """
    The number that makes the case for the whole feature: how much of a trip
    was paid outside its own dates.
    """
    summary = summarize(
        [
            (flight(), 1.0),
            (tx("d", amount=-60.0), 1.0),
            (tx("s", amount=-95.0, date=END + timedelta(days=6)), 1.0),
        ],
        START,
        END,
    )
    assert summary["by_phase"] == {"booked": 380.0, "during": 60.0, "settled": 95.0}
    assert summary["spend"] == 535.0


def test_per_day_counts_both_end_days():
    """A trip from the 4th to the 18th is fifteen days, not fourteen."""
    summary = summarize([(tx("d", amount=-150.0), 1.0)], START, END)
    assert summary["days"] == 15
    assert summary["per_day"] == 10.0


def test_a_trip_with_nothing_assigned_totals_to_nothing():
    """The state every trip starts in; it must not divide by zero."""
    summary = summarize([], START, END)
    assert summary["spend"] == 0.0
    assert summary["net"] == 0.0
    assert summary["per_day"] == 0.0
    assert summary["categories"] == []


# --- Finding the trips nobody has recorded yet -------------------------------
#
# The evidence is that a card reports where it was used. The hard part is not
# spotting a foreign charge, it is refusing the ones that are somebody buying
# something online from a shop that bills from abroad — which, in the real
# data, are the majority of foreign country codes.


def card(tid, day, place="LISBOA", code="351", currency=None, amount=-20.0):
    """A card line in Comdirect's format, with its country code and any
    currency conversion written the way the bank writes them."""
    text = f" MERCADO {place} {code}"
    if currency:
        text += f"Betrag: -{abs(amount) * 4:.2f} {currency}Kurs: 4,0000"
    return tx(tid, amount=amount, date=day, booking_text=text)


def home(tid, day, amount=-9.0):
    """An ordinary charge at home, carrying the home country code."""
    return tx(tid, amount=amount, date=day, booking_text=" EDEKA BOECKER HAMBURG 000 ")


def a_week_at_home(start_day, count=8):
    return [home(f"h{i}", start_day + timedelta(days=i * 3)) for i in range(count)]


def test_the_home_country_is_read_from_the_data_not_assumed():
    """
    The app is not written for one country: the code a card normally reports
    is wherever its holder lives, so it has to be the most common one rather
    than a constant.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2)),
        card("b", datetime(2026, 3, 4)),
    ]
    assert home_country(history) == "000"


def test_a_cluster_of_foreign_charges_is_proposed():
    """The plain case: several charges, close together, over several days."""
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2)),
        card("b", datetime(2026, 3, 3)),
        card("c", datetime(2026, 3, 5)),
    ]
    found = find_trip_candidates(history)
    assert len(found) == 1
    assert found[0].start == datetime(2026, 3, 2)
    assert found[0].end == datetime(2026, 3, 5)
    assert found[0].anchor_id == "a"


def test_a_proposal_carries_no_name():
    """
    The ledger knows the card stopped reporting from home and nothing else.
    Naming the trip is the user's half of the work, and the detector must not
    invent it — there is no field here to put a guess in.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2)),
        card("b", datetime(2026, 3, 4)),
    ]
    found = find_trip_candidates(history)[0]
    assert not hasattr(found, "name")
    assert not hasattr(found, "destination")


def test_a_euro_trip_is_found_by_its_country_code_alone():
    """
    The case foreign currency cannot see. A week in Lisbon converts nothing,
    and without the country code it would look exactly like a week at home —
    which is why both signals are read.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2), code="351"),
        card("b", datetime(2026, 3, 4), code="351"),
        card("c", datetime(2026, 3, 6), code="351"),
    ]
    found = find_trip_candidates(history)
    assert len(found) == 1
    assert found[0].currencies == ()
    assert found[0].countries == ("351",)


def test_a_trip_is_found_by_currency_where_the_line_carries_no_country_code():
    """The mirror case: some card lines in the real data carry a conversion
    and no code at all."""
    abroad = [
        tx(
            f"c{i}",
            date=datetime(2026, 3, 2 + i * 2),
            booking_text=" HOTEL OSLO Betrag: -900,00 NOKKurs: 11,50 ",
        )
        for i in range(3)
    ]
    found = find_trip_candidates(a_week_at_home(datetime(2026, 1, 1)) + abroad)
    assert len(found) == 1
    assert found[0].currencies == ("NOK",)
    assert found[0].countries == ()


def test_scattered_foreign_charges_are_not_a_trip():
    """
    The false positive that dominates the real data: eleven charges under one
    foreign country code spread across eleven months, which is an online shop
    that bills from abroad rather than eleven journeys.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 2, 1)),
        card("b", datetime(2026, 5, 1)),
        card("c", datetime(2026, 9, 1)),
    ]
    assert find_trip_candidates(history) == []


def test_a_lone_pair_on_one_day_is_a_shopping_session():
    """
    Two foreign charges on a single afternoon is an online order and its
    refund, and there is nothing else to say it was anywhere.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2)),
        card("b", datetime(2026, 3, 2)),
    ]
    assert find_trip_candidates(history) == []


def test_a_dense_single_day_is_a_weekend_away():
    """
    Written from the real data, where a Saturday in Denmark left seven
    currency conversions and nothing on any other day. Requiring two days
    refused every weekend trip in the history — seven conversions in an
    afternoon is somebody standing in Denmark, not somebody shopping.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card(f"d{i}", datetime(2026, 3, 2)) for i in range(SAME_DAY_CHARGES)
    ]
    found = find_trip_candidates(history)
    assert len(found) == 1
    assert found[0].days == 1


def test_a_quiet_stretch_inside_a_trip_does_not_split_it():
    """
    Written from the real data, where six days paid in cash split one
    four-week journey into two proposals — and the traveller would then have
    had to dismiss one and widen the other.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2)),
        card("b", datetime(2026, 3, 4)),
        # Six days of cash in between.
        card("c", datetime(2026, 3, 10)),
        card("d", datetime(2026, 3, 12)),
    ]
    found = find_trip_candidates(history)
    assert len(found) == 1
    assert found[0].days == 11


def test_two_separate_journeys_stay_separate():
    """The other side of the same threshold."""
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2)),
        card("b", datetime(2026, 3, 4)),
        card("c", datetime(2026, 6, 2)),
        card("d", datetime(2026, 6, 4)),
    ]
    assert len(find_trip_candidates(history)) == 2


def test_a_thin_cluster_is_proposed_quietly_rather_than_dropped():
    """
    Two charges is what a week in Sweden left behind in the real data, and it
    is also what an online order and its refund leave behind. Dismissing a
    wrong proposal costs one click; a trip never proposed is never noticed, so
    it is graded rather than filtered.
    """
    history = a_week_at_home(datetime(2026, 1, 1)) + [
        card("a", datetime(2026, 3, 2)),
        card("b", datetime(2026, 3, 6)),
    ]
    found = find_trip_candidates(history)
    assert len(found) == 1
    assert found[0].strength == STRENGTH_LIKELY

    dense = a_week_at_home(datetime(2026, 1, 1)) + [
        card(f"x{i}", datetime(2026, 3, 2) + timedelta(days=i)) for i in range(6)
    ]
    assert find_trip_candidates(dense)[0].strength == STRENGTH_STRONG


def test_an_overlapping_window_is_how_a_confirmed_trip_retires_its_proposal():
    """
    There is no second piece of state saying a proposal was accepted. A window
    already covered by a trip simply stops being offered, so the two can never
    fall out of step.
    """
    assert overlaps(
        datetime(2026, 3, 2),
        datetime(2026, 3, 9),
        datetime(2026, 3, 8),
        datetime(2026, 3, 20),
    )
    assert not overlaps(
        datetime(2026, 3, 2),
        datetime(2026, 3, 9),
        datetime(2026, 3, 10),
        datetime(2026, 3, 20),
    )


# --- Splitting a whole trip --------------------------------------------------


def test_splitting_a_trip_scales_every_figure_alike():
    """
    A share of a journey is a share of all of it. The point of scaling at the
    end rather than per entry is that the breakdown keeps its shape: which
    category dominated is not a fact about who paid.
    """
    entries = [
        (tx("a", amount=-600.0, major="Travel", minor="Accommodation"), 1.0),
        (tx("b", amount=-200.0), 1.0),
    ]
    whole = summarize(entries, START, END)
    half = summarize(entries, START, END, split_ways=2)

    assert whole["spend"] == 800.0
    assert half["spend"] == 400.0
    assert half["net"] == 400.0
    assert half["by_phase"]["during"] == whole["by_phase"]["during"] / 2

    # 800 over fifteen days is 53.33 a day and half of that is 26.67, not the
    # 26.66 you get by halving the rounded figure. The split is applied before
    # the single rounding at the end, which is why.
    assert whole["per_day"] == 53.33
    assert half["per_day"] == 26.67

    # The proportions are untouched: accommodation is three quarters of this
    # trip whoever paid for it.
    assert half["categories"][0]["share"] == whole["categories"][0]["share"] == 0.75
    assert half["categories"][0]["amount"] == 300.0


def test_the_full_total_survives_the_split():
    """
    Both numbers are true and a page showing only one of them would be lying
    by omission — a card reporting half of what a holiday cost simply reads as
    a cheaper holiday.
    """
    entries = [(tx("a", amount=-900.0), 1.0)]
    split = summarize(entries, START, END, split_ways=3)
    assert split["net"] == 300.0
    assert split["full_net"] == 900.0
    assert split["split_ways"] == 3


def test_the_two_kinds_of_split_multiply():
    """
    They answer different questions: an entry's share says one payment covered
    somebody not on this trip's books, while the trip split says the whole
    journey was shared. A booking you already only half owned, on a trip you
    then split with a partner, leaves you a quarter.
    """
    booking = tx("a", amount=-800.0, major="Travel", minor="Flights & Long-Distance")
    assert summarize([(booking, 0.5)], START, END, split_ways=2)["spend"] == 200.0


def test_not_splitting_changes_nothing():
    """One is the only value that has to be a no-op, and it is the default."""
    entries = [(tx("a", amount=-120.0), 1.0)]
    assert summarize(entries, START, END) == summarize(entries, START, END, 1)


def test_a_nonsense_split_falls_back_to_the_whole_amount():
    """
    The arithmetic must not be the place a bad value is discovered. Zero or a
    negative would divide a holiday into something meaningless, and the
    service refuses those on the way in — this is the second line.
    """
    entries = [(tx("a", amount=-100.0), 1.0)]
    assert summarize(entries, START, END, split_ways=0)["spend"] == 100.0
    assert summarize(entries, START, END, split_ways=-3)["spend"] == 100.0


# --- Money that moves between people -----------------------------------------
#
# A shared trip is paid for by one person booking and everybody else sending
# them their part. Leaving those transfers out of a trip's total reports
# everybody but the booker as having travelled for free.


def person(tid, amount, day=None, **extra):
    """Money moved between two people rather than paid to a merchant."""
    return tx(
        tid,
        amount=amount,
        date=day or datetime(2026, 7, 10),
        major="Transfers & Savings",
        minor="Person to Person",
        flow_type="transfer",
        booking_text=" Auftraggeber: PayPal Europe S.a.r.l. et Ci ",
        **extra,
    )


def test_sending_your_part_to_whoever_booked_is_what_the_trip_cost_you():
    """
    The case the whole change exists for. Somebody else pays the villa and you
    send them your share; if that transfer does not count, your trip cost
    nothing at all.
    """
    sent = person("p", -450.0)
    assert counts_as_spending(sent)

    summary = summarize([(sent, 1.0)], START, END)
    assert summary["spend"] == 450.0
    assert summary["person_out"] == 450.0
    assert summary["excluded"] == 0.0


def test_being_paid_back_is_money_back_rather_than_negative_spending():
    """
    The other side of the same trip. The person who booked sees the whole cost
    and a pile of money coming back, and both figures are worth having — the
    gross is what the trip cost, the net is what it cost them.
    """
    villa = tx("v", amount=-1200.0, major="Travel", minor="Accommodation")
    back = person("p", 400.0, day=END + timedelta(days=30))

    summary = summarize([(villa, 1.0), (back, 1.0)], START, END)
    assert summary["spend"] == 1200.0
    assert summary["refunds"] == 400.0
    assert summary["person_in"] == 400.0
    assert summary["net"] == 800.0


def test_savings_and_card_settlements_are_still_not_a_holiday():
    """
    Only the two transfer kinds that stand in for real spending count. A
    savings plan that ran mid-trip and a credit-card settlement are still
    money moved between your own accounts.
    """
    savings = tx(
        "s",
        amount=-300.0,
        major="Transfers & Savings",
        minor="Savings Contribution",
        flow_type="transfer",
    )
    card = tx(
        "c",
        amount=-780.0,
        major="Transfers & Savings",
        minor="Credit Card Payment",
        flow_type="transfer",
    )
    assert not counts_as_spending(savings)
    assert not counts_as_spending(card)

    summary = summarize([(savings, 1.0), (card, 1.0)], START, END)
    assert summary["spend"] == 0.0
    assert summary["excluded"] == 1080.0


def test_a_payment_to_a_person_during_the_trip_is_proposed():
    """
    It counts towards the total now, so refusing to ever propose it would
    leave the one thing the total most needs reachable only through search.
    """
    candidate = by_id(find_candidates([person("p", -60.0)], START, END))["p"]
    assert candidate.suggested
    assert candidate.phase == PHASE_DURING


def test_settling_up_long_after_the_return_is_offered_for_the_whole_tail():
    """
    Settling up is the thing that genuinely arrives weeks late, so it is
    offered past the settlement lag that governs card payments — but never
    ticked, because the ledger cannot tell a friend repaying you for the villa
    from one repaying you for a concert.
    """
    late = person("p", 400.0, day=END + timedelta(days=SETTLEMENT_LAG_DAYS + 9))
    candidate = by_id(find_candidates([late], START, END))["p"]
    assert candidate.phase == PHASE_SETTLED
    assert not candidate.suggested
    assert "settling up" in candidate.reason


def test_person_to_person_splits_with_the_trip_like_everything_else():
    """A shared trip's transfers are shared too."""
    sent = person("p", -600.0)
    assert summarize([(sent, 1.0)], START, END, split_ways=2)["person_out"] == 300.0


def test_the_oldest_card_format_is_read_too():
    """
    Comdirect has written card payments three ways across this history, and
    each hides being abroad somewhere else. The oldest writes the country and
    a bare "KURS" and the rate, and it is the only marker there is for 2022
    and most of 2023 — without it the detector cannot see that far back at
    all. A domestic line in the same format has neither.
    """
    abroad = [
        tx(
            f"p{i}",
            date=datetime(2026, 3, 2 + i * 2),
            booking_text=" CAPADLO PRAHA 1 CZ 51,00 KURS 0,041814424 KARTE 0132 ",
        )
        for i in range(3)
    ]
    at_home = tx(
        "h",
        date=datetime(2026, 3, 3),
        booking_text=" ROSSMANN 2200 HAMBURG KARTE 0132 02122022 ",
    )
    found = find_trip_candidates([at_home] + abroad)
    assert len(found) == 1
    assert found[0].countries == ("CZ",)


def test_an_experience_booked_before_departure_is_proposed():
    """
    A walking tour is filed under Events & Culture rather than Travel, so the
    category rule could not see it and a tour booked a fortnight before
    departure was missing from the very window it belongs in.
    """
    tour = tx(
        "g",
        amount=-268.37,
        date=START - timedelta(days=5),
        major="Leisure & Entertainment",
        minor="Events & Culture",
        booking_text=" Auftraggeber: PayPal Europe S.a.r.l. et Cie S.C.A ",
        paypal_counterparty="GETYOURG UIDE DEUTSCHLAND GMBH",
    )
    candidate = by_id(find_candidates([tour], START, END))["g"]
    assert candidate.phase == PHASE_BOOKED
    assert candidate.suggested
    assert "experience" in candidate.reason


def test_a_wrapped_platform_name_is_still_recognised():
    """
    Comdirect wraps these names mid-word and inconsistently — the same
    merchant arrives as "GETYOURG UIDE DEUTSCHLAND GMBH" on one line and
    "GETYOURGUIDE DEUTSC HLAND GMBH" on another — which is exactly what the
    space-free merchant key exists to survive.
    """
    for spelling in (
        "GETYOURG UIDE DEUTSCHLAND GMBH",
        "GETYOURGUIDE DEUTSC HLAND GMBH",
    ):
        assert is_experience_platform(
            tx("x", booking_text=" PayPal ", paypal_counterparty=spelling)
        )


def test_a_small_experience_still_counts_where_a_small_booking_would_not():
    """
    The amount threshold exists to keep a previous holiday's bus fares out of
    the lead-in. A named tour platform needs no such guard — nobody books a
    walking tour by accident — and the cheap ones are exactly what was being
    missed.
    """
    cheap_tour = tx(
        "g",
        amount=-13.35,
        date=START - timedelta(days=20),
        major="Leisure & Entertainment",
        minor="Events & Culture",
        booking_text=" PayPal ",
        paypal_counterparty="VIATOR",
    )
    cheap_flight = flight("f", date=START - timedelta(days=20), amount=-13.35)
    found = by_id(find_candidates([cheap_tour, cheap_flight], START, END))
    assert found["g"].suggested
    assert not found["f"].suggested


def test_an_old_experience_belongs_to_an_earlier_trip():
    """The horizon still applies: a tour booked last autumn was last autumn's."""
    old_tour = tx(
        "g",
        amount=-90.0,
        date=START - timedelta(days=BOOKING_TICK_DAYS + 30),
        major="Leisure & Entertainment",
        minor="Events & Culture",
        booking_text=" PayPal ",
        paypal_counterparty="GETYOURGUIDE",
    )
    assert not by_id(find_candidates([old_tour], START, END))["g"].suggested


def test_ordinary_events_at_home_stay_out_of_the_lead_in():
    """
    The whole category is not admitted, only the named platforms — otherwise a
    year of cinema tickets would bury the lead-in.
    """
    cinema = tx(
        "c",
        amount=-34.0,
        date=START - timedelta(days=30),
        major="Leisure & Entertainment",
        minor="Events & Culture",
        booking_text=" KIEFT & KIEFT FILMTHEATER HAMBURG ",
    )
    assert find_candidates([cinema], START, END) == []
