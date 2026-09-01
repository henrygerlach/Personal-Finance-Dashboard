"""
Tests for the service behind the trips pages.

``app.trips`` decides which transactions a journey could plausibly own, and is
tested on its own. What is defended here is everything the service adds around
it: that a total is assembled from decisions rather than from dates, that one
payment cannot be counted by two trips at once, and that a decision survives
the things which rewrite transactions underneath it.

The documents are stood in for by :mod:`tests.fixtures.odm`, so nothing here
reaches a database.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import services as services_module
from app.models import TRIP_EXCLUDED, TRIP_INCLUDED
from app.services import MAX_SPLIT_WAYS, TripService
from tests.fixtures.odm import document


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch):
    """Empty collections for the four documents the trip service reads."""
    models = SimpleNamespace(
        Trip=document("Trip", "id"),
        TripAssignment=document(
            "TripAssignment", "trip_id", "transaction_id", "decision"
        ),
        Transaction=document("Transaction", "id"),
        DismissedTripProposal=document("DismissedTripProposal", "anchor_id"),
    )
    for name in ("Trip", "TripAssignment", "Transaction", "DismissedTripProposal"):
        monkeypatch.setattr(services_module, name, getattr(models, name))
    return models


def make_trip(db, name="Lisbon", start="2026-07-01", end="2026-07-08", **extra):
    defaults = dict(
        name=name,
        destination="Portugal",
        start_date=datetime.strptime(start, "%Y-%m-%d"),
        end_date=datetime.strptime(end, "%Y-%m-%d"),
        budget=None,
        notes=None,
        split_ways=1,
    )
    defaults.update(extra)
    trip = db.Trip(**defaults)
    db.Trip.rows.append(trip)
    return trip


def make_transaction(
    db, tid, date="2026-07-03", amount=-40.0, text="RESTAURANTE", **extra
):
    defaults = dict(
        id=tid,
        date=datetime.strptime(date, "%Y-%m-%d"),
        amount=amount,
        booking_text=text,
        process=None,
        major_category="Travel",
        minor_category="Accommodation",
        flow_type="expense",
        is_recurring=False,
        paypal_counterparty=None,
    )
    defaults.update(extra)
    transaction = db.Transaction(**defaults)
    db.Transaction.rows.append(transaction)
    return transaction


def assign(db, trip, transaction_id, decision=TRIP_INCLUDED, share=1.0):
    row = db.TripAssignment(
        trip_id=str(trip.id),
        transaction_id=transaction_id,
        decision=decision,
        share=share,
        decided_at=datetime(2026, 8, 1),
    )
    db.TripAssignment.rows.append(row)
    return row


# --- A total is made of decisions, never of dates ----------------------------


def test_a_trip_totals_only_what_was_assigned_to_it(db):
    """
    The whole design turns on this. A flight bought in March and a settlement
    landing in October are the trip's; the rent debited while the flat stood
    empty is not, and no date filter can tell those apart.
    """
    trip = make_trip(db)
    make_transaction(db, "flight", date="2026-03-02", amount=-220.0)
    make_transaction(db, "rent", date="2026-07-02", amount=-900.0)
    assign(db, trip, "flight")

    summary = run(TripService.summary(str(trip.id)))

    assert summary["spend"] == 220.0


def test_a_transaction_inside_the_dates_but_not_assigned_is_not_counted(db):
    """
    Being there at the time is a suggestion, not a decision. This is the case
    a date filter gets wrong in the direction nobody notices.
    """
    trip = make_trip(db)
    make_transaction(db, "groceries", date="2026-07-03", amount=-30.0)

    assert run(TripService.summary(str(trip.id)))["spend"] == 0


def test_a_share_reduces_what_the_trip_owns_of_one_payment(db):
    """
    A house booked for six is not this trip's cost six times over, and the
    booking is one transaction whatever it covered.
    """
    trip = make_trip(db)
    make_transaction(db, "house", amount=-600.0)
    assign(db, trip, "house", share=0.5)

    assert run(TripService.summary(str(trip.id)))["spend"] == 300.0


def test_the_trip_split_and_a_payment_share_compose(db):
    """
    They answer different questions: the share says one payment covered
    somebody not on this trip's books, the split says the whole journey was
    shared. A booking you already only half owned, on a trip split with a
    partner, leaves you a quarter.
    """
    trip = make_trip(db, split_ways=2)
    make_transaction(db, "house", amount=-600.0)
    assign(db, trip, "house", share=0.5)

    assert run(TripService.summary(str(trip.id)))["spend"] == 150.0


def test_an_assignment_whose_transaction_has_gone_is_skipped(db):
    """
    Transactions are rewritten by re-import and the repair scripts, and a
    database restored from a narrower backup is missing some. The decision is
    kept, because the transaction may come back on the next import — but it
    must not be an error in the meantime.
    """
    trip = make_trip(db)
    make_transaction(db, "meal", amount=-40.0)
    assign(db, trip, "meal")
    assign(db, trip, "vanished")

    assert run(TripService.summary(str(trip.id)))["spend"] == 40.0


def test_a_budget_share_is_withheld_rather_than_guessed(db):
    """
    A trip with no budget must not render as one overspent from the first
    euro, so the figure is absent rather than zero or infinite.
    """
    trip = make_trip(db, budget=None)
    with_budget = make_trip(db, name="Rome", budget=1000.0)
    make_transaction(db, "meal", amount=-250.0)
    assign(db, with_budget, "meal")

    assert run(TripService.summary(str(trip.id)))["budget_share"] is None
    assert run(TripService.summary(str(with_budget.id)))["budget_share"] == 0.25


# --- Creating and rewriting ---------------------------------------------------


def test_a_trip_needs_a_name(db):
    """
    A proposal carries no name because the ledger does not know one; supplying
    it is the user's half of the work and what makes confirming a decision.
    """
    with pytest.raises(ValueError, match="needs a name"):
        run(TripService.create("  ", datetime(2026, 7, 1), datetime(2026, 7, 8)))


def test_a_trip_cannot_end_before_it_starts(db):
    with pytest.raises(ValueError, match="cannot end before"):
        run(TripService.create("Lisbon", datetime(2026, 7, 8), datetime(2026, 7, 1)))


def test_a_one_day_trip_is_allowed(db):
    """A day trip is a trip, and the check is on the wrong order, not on length."""
    trip = run(TripService.create("Bruges", datetime(2026, 7, 1), datetime(2026, 7, 1)))

    assert trip.name == "Bruges"


def test_an_empty_destination_and_note_are_stored_as_absent(db):
    """
    The form always sends the fields. Empty strings would make the map try to
    resolve "" and the page render a blank note block.
    """
    trip = run(
        TripService.create(
            "Lisbon",
            datetime(2026, 7, 1),
            datetime(2026, 7, 8),
            destination="   ",
            notes="",
        )
    )

    assert trip.destination is None
    assert trip.notes is None


def test_a_budget_of_zero_is_no_budget(db):
    """Zero would mean every trip is instantly over budget."""
    trip = run(
        TripService.create(
            "Lisbon", datetime(2026, 7, 1), datetime(2026, 7, 8), budget=0
        )
    )

    assert trip.budget is None


def test_moving_the_dates_touches_no_assignment(db):
    """
    A date correction is not a retraction of a decision the user already made.
    Re-running the suggester is the point; discarding their work is not.
    """
    trip = make_trip(db)
    make_transaction(db, "flight", date="2026-03-02", amount=-220.0)
    assign(db, trip, "flight")

    run(
        TripService.update(
            str(trip.id), "Lisbon", datetime(2026, 7, 2), datetime(2026, 7, 10)
        )
    )

    assert run(TripService.summary(str(trip.id)))["spend"] == 220.0


def test_an_update_holds_the_same_rules_as_a_creation(db):
    trip = make_trip(db)

    with pytest.raises(ValueError, match="needs a name"):
        run(
            TripService.update(
                str(trip.id), "", datetime(2026, 7, 1), datetime(2026, 7, 8)
            )
        )
    with pytest.raises(ValueError, match="cannot end before"):
        run(
            TripService.update(
                str(trip.id), "Lisbon", datetime(2026, 7, 8), datetime(2026, 7, 1)
            )
        )


def test_deleting_a_trip_takes_its_decisions_with_it(db):
    """
    Assignments outlive the transactions they point at by design. Outliving
    the trip would leave rows nothing can reach or clean up.
    """
    trip = make_trip(db)
    other = make_trip(db, name="Rome")
    make_transaction(db, "meal")
    assign(db, trip, "meal")
    assign(db, other, "meal")

    removed = run(TripService.delete(str(trip.id)))

    assert removed == 1
    assert [row.trip_id for row in db.TripAssignment.rows] == [str(other.id)]


def test_an_id_that_is_not_an_id_at_all_is_a_value_error(db):
    """
    The route turns this into a 404. Letting the object-id parse fail would
    make a stale bookmark a 500.
    """
    with pytest.raises(ValueError):
        run(TripService.detail("not-an-object-id"))


# --- One payment, one trip ----------------------------------------------------


def test_including_a_transaction_another_trip_holds_moves_it(db):
    """
    Two trips counting one flight is plausible in each and a lie across both.
    Refusing would leave the only way out being to go and un-assign it from
    the other trip first — a correct answer nobody would find.
    """
    lisbon = make_trip(db, name="Lisbon")
    rome = make_trip(db, name="Rome", start="2026-09-01", end="2026-09-08")
    make_transaction(db, "flight", date="2026-06-01", amount=-220.0)
    assign(db, lisbon, "flight")

    result = run(TripService.assign(str(rome.id), "flight", TRIP_INCLUDED))

    assert result["moved_from"] == "Lisbon"
    assert run(TripService.summary(str(lisbon.id)))["spend"] == 0
    assert run(TripService.summary(str(rome.id)))["spend"] == 220.0


def test_reassigning_within_the_same_trip_is_not_a_move(db):
    """Changing a share must not report the trip as stealing from itself."""
    trip = make_trip(db)
    make_transaction(db, "house", amount=-600.0)
    assign(db, trip, "house")

    result = run(TripService.assign(str(trip.id), "house", TRIP_INCLUDED, share=0.5))

    assert result["moved_from"] is None
    assert run(TripService.summary(str(trip.id)))["spend"] == 300.0


def test_an_unknown_decision_is_refused(db):
    trip = make_trip(db)

    with pytest.raises(ValueError, match="Unknown decision"):
        run(TripService.assign(str(trip.id), "x", "perhaps"))


@pytest.mark.parametrize("share", [0, -0.5, 1.5])
def test_a_share_outside_the_range_is_refused(db, share):
    """
    A share is a fraction of one payment. Above one it invents money, at or
    below zero it is an exclusion wearing an inclusion's name.
    """
    trip = make_trip(db)

    with pytest.raises(ValueError, match="share"):
        run(TripService.assign(str(trip.id), "x", TRIP_INCLUDED, share=share))


def test_excluding_is_an_answer_and_clearing_forgets_the_question(db):
    """
    An exclusion stops the suggester asking; clearing hands the transaction
    back to it. Collapsing the two would make "no" unrecordable.
    """
    trip = make_trip(db)
    make_transaction(db, "meal")

    run(TripService.assign(str(trip.id), "meal", TRIP_EXCLUDED))
    assert len(db.TripAssignment.rows) == 1

    cleared = run(TripService.clear(str(trip.id), "meal"))
    assert cleared["cleared"] is True
    assert db.TripAssignment.rows == []


def test_clearing_a_decision_that_was_never_made_says_so(db):
    trip = make_trip(db)

    assert run(TripService.clear(str(trip.id), "never"))["cleared"] is False


def test_assigning_a_selection_writes_exactly_that_selection(db):
    """
    The previous version assigned everything the detector had proposed and
    called them ticked, so working down the list and then pressing the button
    swept in every row that had not been answered.
    """
    trip = make_trip(db)
    make_transaction(db, "a", amount=-10.0)
    make_transaction(db, "b", amount=-20.0)
    make_transaction(db, "c", amount=-40.0)

    written = run(TripService.assign_many(str(trip.id), ["a", "c"]))

    assert written == 2
    assert run(TripService.summary(str(trip.id)))["spend"] == 50.0


# --- Splitting ----------------------------------------------------------------


def test_a_split_halves_the_total_without_touching_a_share(db):
    """
    Splitting is a fact about the trip. Applied by rewriting every assignment
    it would overwrite the shares set by hand with no way back.
    """
    trip = make_trip(db)
    make_transaction(db, "house", amount=-600.0)
    assign(db, trip, "house", share=1.0)

    run(TripService.set_split(str(trip.id), 2))

    assert db.TripAssignment.rows[0].share == 1.0
    assert run(TripService.summary(str(trip.id)))["spend"] == 300.0


def test_splitting_one_way_puts_the_whole_total_back(db):
    """Undoing a split has to be one click, not a repair job."""
    trip = make_trip(db, split_ways=4)
    make_transaction(db, "house", amount=-600.0)
    assign(db, trip, "house")

    run(TripService.set_split(str(trip.id), 1))

    assert run(TripService.summary(str(trip.id)))["spend"] == 600.0


@pytest.mark.parametrize("ways", [0, -1, MAX_SPLIT_WAYS + 1])
def test_an_impossible_split_is_refused(db, ways):
    """Zero people divides by zero, and a coach party is a data entry slip."""
    trip = make_trip(db)

    with pytest.raises(ValueError, match="split between"):
        run(TripService.set_split(str(trip.id), ways))


# --- The overview -------------------------------------------------------------


def test_the_overview_of_no_trips_is_empty_rather_than_absent(db):
    """The page renders the totals unconditionally."""
    assert run(TripService.overview()) == {
        "trips": [],
        "count": 0,
        "total_net": 0.0,
        "total_spend": 0.0,
    }


def test_the_overview_lists_trips_by_departure_newest_first(db):
    """A list of holidays is read from the most recent one backwards."""
    make_trip(db, name="Old", start="2025-01-01", end="2025-01-08")
    make_trip(db, name="New", start="2026-07-01", end="2026-07-08")

    overview = run(TripService.overview())

    assert [item["name"] for item in overview["trips"]] == ["New", "Old"]


def test_the_overview_totals_across_every_trip(db):
    """What travelling has cost, which is the number the page leads with."""
    lisbon = make_trip(db, name="Lisbon")
    rome = make_trip(db, name="Rome", start="2026-09-01", end="2026-09-08")
    make_transaction(db, "a", amount=-100.0)
    make_transaction(db, "b", date="2026-09-02", amount=-250.0)
    assign(db, lisbon, "a")
    assign(db, rome, "b")

    overview = run(TripService.overview())

    assert overview["count"] == 2
    assert overview["total_spend"] == 350.0


def test_the_overview_names_the_headline_category_of_each_trip(db):
    """It is what a card can say about a trip in one line."""
    trip = make_trip(db)
    make_transaction(db, "hotel", amount=-400.0, minor_category="Accommodation")
    assign(db, trip, "hotel")

    overview = run(TripService.overview())

    assert overview["trips"][0]["top_category"] == "Accommodation"


def test_a_trip_with_nothing_assigned_has_no_headline_category(db):
    """A freshly created trip is the normal case, not an edge one."""
    make_trip(db)

    assert run(TripService.overview())["trips"][0]["top_category"] is None


# --- The detail page ----------------------------------------------------------


def test_the_detail_lists_assignments_newest_first(db):
    """
    The summary walks the trip forwards; a reader scans the list backwards.
    Both orders are deliberate and they are not the same order.
    """
    trip = make_trip(db)
    make_transaction(db, "early", date="2026-07-02", amount=-10.0)
    make_transaction(db, "late", date="2026-07-06", amount=-20.0)
    assign(db, trip, "early")
    assign(db, trip, "late")

    detail = run(TripService.detail(str(trip.id)))

    assert [row["date"] for row in detail["assigned"]] == ["2026-07-06", "2026-07-02"]


def test_an_assigned_transaction_is_not_offered_in_the_queue_as_well(db):
    """It is in the list above; offering it again is asking a settled question."""
    trip = make_trip(db)
    make_transaction(db, "meal", date="2026-07-03", amount=-40.0)
    assign(db, trip, "meal")

    detail = run(TripService.detail(str(trip.id)))

    queued = [c["id"] for phase in detail["candidates"].values() for c in phase]
    assert "meal" not in queued


def test_a_transaction_another_trip_holds_is_shown_as_claimed(db):
    """
    Shown rather than hidden: the person needs to know why a line they expect
    to see is not being offered.
    """
    lisbon = make_trip(db, name="Lisbon")
    rome = make_trip(db, name="Rome", start="2026-07-04", end="2026-07-20")
    make_transaction(db, "meal", date="2026-07-05", amount=-40.0)
    assign(db, lisbon, "meal")

    detail = run(TripService.detail(str(rome.id)))

    claimed = [
        c for phase in detail["candidates"].values() for c in phase if c["id"] == "meal"
    ]
    assert claimed and claimed[0]["claimed_by"] == "Lisbon"


def test_a_charge_made_during_another_journey_is_not_ticked_for_this_one(db):
    """
    Travel categories hold two different things: a booking made from home and
    a fortnight paid for on the ground. Without this, last autumn's entire
    trip is proposed as this summer's bookings.
    """
    make_trip(db, name="Vietnam", start="2026-02-01", end="2026-02-20")
    summer = make_trip(db, name="Lisbon", start="2026-07-01", end="2026-07-08")
    make_transaction(
        db, "tour", date="2026-02-10", amount=-300.0, minor_category="Package Tours"
    )

    detail = run(TripService.detail(str(summer.id)))

    offered = [
        c for phase in detail["candidates"].values() for c in phase if c["id"] == "tour"
    ]
    for row in offered:
        assert row["suggested"] is False
        assert row["during_other"] == "Vietnam"


def test_the_detail_counts_what_is_waiting_to_be_decided(db):
    """The queue at the foot of the page is the whole point of it."""
    trip = make_trip(db)
    make_transaction(db, "meal", date="2026-07-03", amount=-40.0)

    detail = run(TripService.detail(str(trip.id)))

    assert detail["candidate_count"] >= 1
    assert detail["suggested_count"] >= 1


def test_a_trip_that_does_not_exist_raises(db):
    with pytest.raises(ValueError, match="No trip"):
        run(TripService.detail("68af1e2c9b4d5a6f7c8d9e01"))


# --- Search -------------------------------------------------------------------


def test_search_matches_the_bank_s_own_line_and_the_merchant(db):
    """
    The merchant is derived and the booking line is what was recorded. A
    search over only one of them misses whichever the user remembers.
    """
    trip = make_trip(db)
    make_transaction(db, "a", text="RYANAIR DUBLIN", amount=-90.0)
    make_transaction(db, "b", text="Auftraggeber: EDEKA", amount=-12.0)

    results = run(TripService.search(str(trip.id), "ryanair"))

    assert [row["id"] for row in results] == ["a"]


def test_search_is_case_insensitive(db):
    """Booking lines are upper case and nobody types them that way."""
    trip = make_trip(db)
    make_transaction(db, "a", text="RYANAIR DUBLIN", amount=-90.0)

    assert len(run(TripService.search(str(trip.id), "RyanAir"))) == 1


def test_a_one_character_search_returns_nothing(db):
    """
    A single letter matches most of a ledger, and the result would be a list
    nobody can use rather than an error.
    """
    trip = make_trip(db)
    make_transaction(db, "a", text="RYANAIR", amount=-90.0)

    assert run(TripService.search(str(trip.id), "r")) == []


def test_search_returns_the_most_recent_matches_first(db):
    """The line being looked for is usually the one just imported."""
    trip = make_trip(db)
    make_transaction(db, "old", date="2024-01-01", text="RYANAIR", amount=-90.0)
    make_transaction(db, "new", date="2026-06-01", text="RYANAIR", amount=-95.0)

    assert [row["id"] for row in run(TripService.search(str(trip.id), "ryanair"))] == [
        "new",
        "old",
    ]


def test_search_says_which_of_its_results_this_trip_already_holds(db):
    """Otherwise the only way to find out is to assign it and see nothing change."""
    trip = make_trip(db)
    make_transaction(db, "a", text="RYANAIR", amount=-90.0)
    assign(db, trip, "a")

    row = run(TripService.search(str(trip.id), "ryanair"))[0]

    assert row["assigned"] is True
    assert row["claimed_by"] is None


# --- Proposals ----------------------------------------------------------------


def test_a_window_a_trip_already_covers_is_not_proposed(db):
    """
    Confirming a proposal is all it takes to retire it. There is no second
    piece of state saying it was accepted, and so none to fall out of step.
    """
    for day in range(6):
        make_transaction(
            db,
            f"abroad{day}",
            date=f"2026-07-0{day + 2}",
            amount=-30.0,
            text=f"KAUFHAUS LISSABON PT {day}",
        )

    before = run(TripService.proposals())
    make_trip(db, start="2026-07-01", end="2026-07-08")
    after = run(TripService.proposals())

    assert len(after["proposals"]) <= len(before["proposals"])


def test_a_refused_proposal_is_not_offered_again(db):
    """
    Detection runs on every load, so without this the same wrongly flagged
    week comes back after every upload.
    """
    dismissed = db.DismissedTripProposal(
        anchor_id="nope", start_date=datetime(2026, 7, 1), end_date=datetime(2026, 7, 8)
    )
    db.DismissedTripProposal.rows.append(dismissed)

    result = run(TripService.proposals())

    assert all(item["anchor_id"] != "nope" for item in result["proposals"])


def test_refusing_a_proposal_that_detection_does_not_produce_is_an_error(db):
    """Storing a refusal of nothing leaves a row that can never be undone."""
    with pytest.raises(ValueError, match="No detected trip"):
        run(TripService.dismiss_proposal("invented"))


def test_refusing_the_same_proposal_twice_is_not_a_second_refusal(db):
    """Two rows for one refusal would take two clicks to undo."""
    db.DismissedTripProposal.rows.append(
        db.DismissedTripProposal(
            anchor_id="x",
            start_date=datetime(2026, 7, 1),
            end_date=datetime(2026, 7, 8),
        )
    )

    assert run(TripService.dismiss_proposal("x")) is False


def test_a_refusal_can_be_undone(db):
    db.DismissedTripProposal.rows.append(
        db.DismissedTripProposal(
            anchor_id="x",
            start_date=datetime(2026, 7, 1),
            end_date=datetime(2026, 7, 8),
        )
    )

    assert run(TripService.restore_proposal("x")) is True
    assert db.DismissedTripProposal.rows == []


def test_undoing_a_refusal_that_was_never_made_says_so(db):
    assert run(TripService.restore_proposal("never")) is False


def test_the_proposals_say_what_the_detector_cannot_see(db):
    """
    A reader told "we found your trips" assumes the ones missing did not
    happen, so the page has to name the limit out loud.
    """
    make_transaction(db, "a", text="EDEKA", amount=-20.0)

    assert "home_country" in run(TripService.proposals())


# --- What a proposal carries --------------------------------------------------


def card_line(db, tid, day, place="LISBOA", code="351", amount=-20.0):
    """A card line in Comdirect's format, with the country code it stamps."""
    return make_transaction(
        db,
        tid,
        date=day,
        amount=amount,
        text=f" MERCADO {place} {code}",
        minor_category="Restaurants & Bars",
    )


def at_home(db, tid, day, amount=-9.0):
    """An ordinary charge carrying the country code the card usually reports."""
    return make_transaction(
        db, tid, date=day, amount=amount, text=" EDEKA BOECKER HAMBURG 000 "
    )


def a_week_abroad(db):
    """Enough of a history for the detector to have a home country and a trip."""
    for index in range(8):
        at_home(db, f"h{index}", f"2026-05-{index * 3 + 1:02d}")
    for index in range(4):
        card_line(db, f"a{index}", f"2026-07-{index + 2:02d}")


def test_a_proposal_carries_the_evidence_behind_it(db):
    """
    A proposal is a claim about a stretch of history, and it has to arrive
    with the transactions that make the claim rather than behind a second
    request the reader has to ask for.
    """
    a_week_abroad(db)

    result = run(TripService.proposals())

    assert result["proposals"]
    proposal = result["proposals"][0]
    assert proposal["charges"] >= 2
    assert proposal["transactions"]
    assert proposal["countries"]


def test_a_proposal_is_named_by_its_merchants_rather_than_its_country_code(db):
    """
    A country code is evidence; "MERCADO LISBOA" is a memory. The merchants
    are what a reader actually recognises a trip by.
    """
    a_week_abroad(db)

    proposal = run(TripService.proposals())["proposals"][0]

    assert any("LISBOA" in name for name in proposal["merchants"])


def test_a_proposal_carries_no_name(db):
    """
    The ledger knows the card stopped reporting from home and nothing else, so
    naming the trip is the user's half of the work — which is also what makes
    confirming it a decision rather than a formality.
    """
    a_week_abroad(db)

    assert "name" not in run(TripService.proposals())["proposals"][0]


def test_a_proposal_can_be_refused_and_then_offered_again(db):
    """
    Detection runs on every load, so a refusal has to be stored — and undoing
    it has to put the proposal back.
    """
    a_week_abroad(db)
    anchor = run(TripService.proposals())["proposals"][0]["anchor_id"]

    assert run(TripService.dismiss_proposal(anchor)) is True
    after = run(TripService.proposals())
    assert all(item["anchor_id"] != anchor for item in after["proposals"])
    assert any(item["anchor_id"] == anchor for item in after["dismissed"])

    assert run(TripService.restore_proposal(anchor)) is True
    assert any(
        item["anchor_id"] == anchor
        for item in run(TripService.proposals())["proposals"]
    )


def test_confirming_a_trip_retires_the_proposal_that_led_to_it(db):
    """
    A window a trip already covers is not a proposal, so there is no second
    piece of state saying it was accepted and none to fall out of step.
    """
    a_week_abroad(db)
    assert run(TripService.proposals())["proposals"]

    make_trip(db, start="2026-07-01", end="2026-07-08")

    assert run(TripService.proposals())["proposals"] == []
