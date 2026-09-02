"""
One journey: what it cost, and what it may be missing.
"""


from collections import Counter
from datetime import datetime, timedelta

from app.config import get_logger
from app.models import (
    TRIP_DECISIONS,
    TRIP_INCLUDED,
    DismissedTripProposal,
    Transaction,
    Trip,
    TripAssignment,
    object_id,
)
from app.normalization import merchant_of
from app.places import atlas
from app.services.common import _describe_transaction
from app.trips import (
    CONVERTED_MARKER,
    PHASE_BOOKED,
    PHASES,
    SETTLEMENT_TAIL_DAYS,
    assigned_phase,
    counts_as_spending,
    find_candidates,
    find_trip_candidates,
    home_country,
    overlaps,
    summarize,
)

logger = get_logger(__name__)

logger = get_logger(__name__)


#: The largest group a trip can be split between. Not a real limit on
#: friendship, just a guard so a stray keystroke cannot divide a holiday by
#: nine thousand and quietly report that it was free.
MAX_SPLIT_WAYS = 50

#: How many transactions the manual search returns. The search exists to reach
#: the one line the suggester's windows did not cover, not to browse the
#: ledger, so a short list is the honest length.
TRIP_SEARCH_RESULTS = 25

#: How many transactions the search reads at a time. It walks the ledger from
#: the recent end and stops as soon as it has filled the result list, so a
#: search for something bought last month reads one page rather than every
#: transaction ever imported. It cannot ask MongoDB to do the matching: half
#: of the predicate is the merchant, which is derived from the booking line
#: rather than stored, and the two differ — the normalizer repairs the
#: encoding damage in the export, so "F ÜR" is searchable as "FUER" only
#: after it has been through Python.
TRIP_SEARCH_PAGE = 500


def _trip_window(trip) -> tuple[datetime, datetime]:
    """The trip's own days, as the two datetimes every phase question uses."""
    return trip.start_date, trip.end_date


class TripService:
    """
    What a journey cost, assembled from decisions rather than from dates.

    The whole design turns on one point: a trip's spending is not contained in
    its dates, so nothing here derives a total from them. The dates feed
    :mod:`app.trips`, which *proposes* transactions; the totals are computed
    only over the ones that were then assigned by hand.

    Every read loads the whole transaction history once and indexes it by id,
    the way :class:`RecurringService` does. Assignments can point anywhere —
    a flight booked fourteen months ahead, a friend settling up in the autumn
    — so a range query would have to be widened to the point of pointlessness
    to stay correct.
    """

    @staticmethod
    async def _assigned_rows(trip, by_id: dict) -> list[tuple]:
        """
        The ``(transaction, share)`` pairs one trip owns, oldest first.

        An assignment whose transaction has since vanished — a database
        restored from a narrower backup, a row deleted by hand — is skipped
        rather than being an error. The decision stays stored, because the
        transaction may well come back on the next import.
        """
        assignments = await TripAssignment.find(
            TripAssignment.trip_id == str(trip.id),
            TripAssignment.decision == TRIP_INCLUDED,
        ).to_list()
        rows = [
            (by_id[a.transaction_id], a.share)
            for a in assignments
            if a.transaction_id in by_id
        ]
        return sorted(rows, key=lambda row: row[0].date)

    @staticmethod
    async def overview() -> dict:
        """
        Every trip with what it cost, most recent departure first.

        Returns:
            The trips, and the totals across all of them. The totals are a
            sum of nets: what travelling has actually cost, after everything
            that came back.
        """
        trips = await Trip.find_all().sort("-start_date").to_list()
        if not trips:
            return {"trips": [], "count": 0, "total_net": 0.0, "total_spend": 0.0}

        transactions = await Transaction.find_all().to_list()
        by_id = {str(t.id): t for t in transactions}

        items = []
        for trip in trips:
            start, end = _trip_window(trip)
            rows = await TripService._assigned_rows(trip, by_id)
            summary = summarize(rows, start, end, trip.split_ways)
            items.append(
                {
                    "id": str(trip.id),
                    "name": trip.name,
                    "destination": trip.destination,
                    "start_date": start.strftime("%Y-%m-%d"),
                    "end_date": end.strftime("%Y-%m-%d"),
                    "budget": trip.budget,
                    "split_ways": trip.split_ways,
                    # Only meaningful against a budget, so it is withheld
                    # rather than guessed when there is none.
                    "budget_share": (
                        round(summary["net"] / trip.budget, 4) if trip.budget else None
                    ),
                    "summary": summary,
                    # The headline category, which is what a card can say
                    # about a trip in one line.
                    "top_category": (
                        summary["categories"][0]["minor"]
                        if summary["categories"]
                        else None
                    ),
                }
            )

        return {
            "trips": items,
            "count": len(items),
            "total_net": round(sum(i["summary"]["net"] for i in items), 2),
            "total_spend": round(sum(i["summary"]["spend"] for i in items), 2),
            # Where the trips were, resolved from their destinations against
            # an offline gazetteer. Built here rather than in a second route
            # because it is the same list of trips seen from above.
            "atlas": atlas(items),
        }

    @staticmethod
    async def detail(trip_id: str) -> dict:
        """
        One trip: what it cost, what is assigned to it, and what it might miss.

        Raises:
            ValueError: No trip answers to that id.
        """
        trip = await TripService._get(trip_id)
        start, end = _trip_window(trip)

        transactions = await Transaction.find_all().to_list()
        by_id = {str(t.id): t for t in transactions}

        assignments = await TripAssignment.find(
            TripAssignment.trip_id == str(trip.id)
        ).to_list()
        decided = {a.transaction_id: a for a in assignments}

        rows = [
            (by_id[a.transaction_id], a.share)
            for a in assignments
            if a.decision == TRIP_INCLUDED and a.transaction_id in by_id
        ]
        rows.sort(key=lambda row: row[0].date)
        summary = summarize(rows, start, end, trip.split_ways)

        # Which transactions other trips have already claimed. One booking
        # counted by two trips would be plausible in each and a lie across
        # both, so a claimed line is shown as claimed rather than offered.
        claims = await TripService._claims(exclude_trip_id=str(trip.id))
        all_trips = await Trip.find_all().to_list()
        trip_names = {str(t.id): t.name for t in all_trips}

        assigned = []
        for transaction, share in rows:
            described = _describe_transaction(transaction)
            described["share"] = share
            described["phase"] = assigned_phase(transaction.date, start, end)
            described["counted"] = counts_as_spending(transaction)
            assigned.append(described)

        # Newest first in the list, oldest first in the arithmetic: the
        # summary walks the trip forwards, a reader scans it backwards.
        assigned.reverse()

        # The windows of every *other* trip. A charge made while you were away
        # somewhere else was that journey's, whatever its category — the
        # lead-in otherwise offers last autumn's walking tours as bookings for
        # this spring's holiday, which is precisely what it looks like from
        # the dates alone.
        others = [
            (other.name, other.start_date, other.end_date)
            for other in all_trips
            if str(other.id) != str(trip.id)
        ]

        def better_claim(when: datetime, phase: str) -> str | None:
            """
            The other trip with a stronger claim on this charge, if any.

            Three ways another journey can own a payment that the dates would
            otherwise offer to this one. It can have happened *during* that
            trip; it can have landed in that trip's settlement tail, the way a
            card payment abroad reaches the bank days later; or — for
            something bought before departure — that trip can simply leave
            sooner. A tour bought five days before flying to Vietnam is
            Vietnam's, even though it is also, technically, five months before
            the next holiday.
            """
            tail = timedelta(days=SETTLEMENT_TAIL_DAYS)
            for name, other_start, other_end in others:
                if other_start <= when <= other_end:
                    return name
                if other_end < when <= other_end + tail:
                    return name
                if phase == PHASE_BOOKED and when < other_start < start:
                    return name
            return None

        candidates = []
        for candidate in find_candidates(transactions, start, end):
            existing = decided.get(candidate.transaction_id)
            if existing and existing.decision == TRIP_INCLUDED:
                # Already assigned; it is in the list above, not the queue.
                continue
            transaction = by_id[candidate.transaction_id]
            described = _describe_transaction(transaction)
            elsewhere = better_claim(transaction.date, candidate.phase)
            described.update(
                {
                    "phase": candidate.phase,
                    "strength": candidate.strength,
                    "reason": candidate.reason,
                    # A decision already made outranks the suggestion, and so
                    # does having happened during another journey. Both leave
                    # the row in the queue — they only refuse to tick it.
                    "suggested": (
                        candidate.suggested and existing is None and not elsewhere
                    ),
                    "dismissed": bool(existing),
                    "claimed_by": trip_names.get(claims.get(candidate.transaction_id)),
                    "during_other": elsewhere,
                }
            )
            candidates.append(described)

        by_phase = {
            phase: [c for c in candidates if c["phase"] == phase] for phase in PHASES
        }

        return {
            "id": str(trip.id),
            "name": trip.name,
            "destination": trip.destination,
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": end.strftime("%Y-%m-%d"),
            "budget": trip.budget,
            "budget_share": (
                round(summary["net"] / trip.budget, 4) if trip.budget else None
            ),
            "notes": trip.notes,
            "split_ways": trip.split_ways,
            "summary": summary,
            "assigned": assigned,
            "candidates": by_phase,
            "candidate_count": len(candidates),
            "suggested_count": sum(1 for c in candidates if c["suggested"]),
            "dismissed_count": sum(1 for c in candidates if c["dismissed"]),
        }

    @staticmethod
    async def _get(trip_id: str) -> Trip:
        """One trip by id, or a ValueError the route can turn into a 404."""
        try:
            trip = await Trip.get(object_id(trip_id))
        except ValueError:  # an id that is not an object id at all
            trip = None
        if trip is None:
            raise ValueError(f"No trip {trip_id!r}")
        return trip

    @staticmethod
    async def _claims(exclude_trip_id: str | None = None) -> dict[str, str]:
        """Transaction id -> the trip that has already included it."""
        included = await TripAssignment.find(
            TripAssignment.decision == TRIP_INCLUDED
        ).to_list()
        return {
            a.transaction_id: a.trip_id
            for a in included
            if a.trip_id != exclude_trip_id
        }

    @staticmethod
    async def create(
        name: str,
        start_date: datetime,
        end_date: datetime,
        destination: str | None = None,
        budget: float | None = None,
        notes: str | None = None,
    ) -> Trip:
        """
        Store a new trip.

        Raises:
            ValueError: The name is empty or the trip ends before it starts.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("A trip needs a name")
        if end_date < start_date:
            raise ValueError("A trip cannot end before it starts")

        trip = Trip(
            name=name,
            destination=(destination or "").strip() or None,
            start_date=start_date,
            end_date=end_date,
            budget=budget if budget and budget > 0 else None,
            notes=(notes or "").strip() or None,
        )
        await trip.insert()
        logger.info(
            "Trip created: %s (%s to %s)",
            name,
            start_date.date(),
            end_date.date(),
        )
        return trip

    @staticmethod
    async def update(
        trip_id: str,
        name: str,
        start_date: datetime,
        end_date: datetime,
        destination: str | None = None,
        budget: float | None = None,
        notes: str | None = None,
    ) -> Trip:
        """
        Rewrite one trip.

        Moving the dates re-runs the suggester but touches no assignment: what
        the user has already decided is theirs, and a date correction is not a
        retraction of it.
        """
        trip = await TripService._get(trip_id)
        name = (name or "").strip()
        if not name:
            raise ValueError("A trip needs a name")
        if end_date < start_date:
            raise ValueError("A trip cannot end before it starts")

        trip.name = name
        trip.destination = (destination or "").strip() or None
        trip.start_date = start_date
        trip.end_date = end_date
        trip.budget = budget if budget and budget > 0 else None
        trip.notes = (notes or "").strip() or None
        await trip.save()
        return trip

    @staticmethod
    async def delete(trip_id: str) -> int:
        """
        Remove a trip and every decision made about it.

        Returns:
            How many assignments went with it.
        """
        trip = await TripService._get(trip_id)
        assignments = await TripAssignment.find(
            TripAssignment.trip_id == str(trip.id)
        ).to_list()
        for assignment in assignments:
            await assignment.delete()
        await trip.delete()
        logger.info("Trip deleted: %s (%s assignments)", trip.name, len(assignments))
        return len(assignments)

    @staticmethod
    async def assign(
        trip_id: str,
        transaction_id: str,
        decision: str,
        share: float = 1.0,
    ) -> dict:
        """
        Record what the user says about one transaction and one trip.

        Including a transaction another trip already holds *moves* it rather
        than refusing. Two trips counting one flight would each be plausible
        alone and wrong together, and refusing would leave the only way out
        being to go and un-assign it from the other trip first — a correct
        answer nobody would find. The move is reported back so the page can
        say what happened.

        Args:
            trip_id: The trip being decided about.
            transaction_id: The transaction being decided on.
            decision: ``included`` or ``excluded``.
            share: The fraction of the amount this trip owns, 0 to 1.

        Returns:
            The trip's recomputed summary, and the name of the trip the
            transaction was taken from, if any.

        Raises:
            ValueError: Unknown trip, unknown decision, or a share outside 0-1.
        """
        trip = await TripService._get(trip_id)
        moved_from = await TripService._write(trip, transaction_id, decision, share)
        return {
            "moved_from": moved_from,
            "summary": await TripService.summary(trip_id),
        }

    @staticmethod
    async def _write(
        trip, transaction_id: str, decision: str, share: float = 1.0
    ) -> str | None:
        """
        Store one decision, without recomputing anything.

        Split out from :meth:`assign` so that accepting a whole queue of
        suggestions writes them all and totals the trip once, rather than
        re-reading the history after every single one.

        Returns:
            The name of the trip the transaction was taken from, if it had to
            be moved.

        Raises:
            ValueError: Unknown decision, or a share outside 0-1.
        """
        if decision not in TRIP_DECISIONS:
            raise ValueError(f"Unknown decision {decision!r}")
        if not 0 < share <= 1:
            raise ValueError("A share must be greater than 0 and at most 1")

        moved_from = None
        if decision == TRIP_INCLUDED:
            elsewhere = await TripAssignment.find(
                TripAssignment.transaction_id == transaction_id,
                TripAssignment.decision == TRIP_INCLUDED,
            ).to_list()
            for held in elsewhere:
                if held.trip_id == str(trip.id):
                    continue
                try:
                    other = await Trip.get(object_id(held.trip_id))
                except ValueError:
                    other = None
                moved_from = other.name if other else None
                await held.delete()

        existing = await TripAssignment.find_one(
            TripAssignment.trip_id == str(trip.id),
            TripAssignment.transaction_id == transaction_id,
        )
        if existing is None:
            await TripAssignment(
                trip_id=str(trip.id),
                transaction_id=transaction_id,
                decision=decision,
                share=share,
            ).insert()
        else:
            existing.decision = decision
            existing.share = share
            existing.decided_at = datetime.utcnow()
            await existing.save()
        return moved_from

    @staticmethod
    async def clear(trip_id: str, transaction_id: str) -> dict:
        """
        Undo a decision, handing the transaction back to the suggester.

        Not the same as excluding it: an exclusion is an answer and stops the
        suggester asking, while clearing forgets the question was ever put.
        """
        trip = await TripService._get(trip_id)
        existing = await TripAssignment.find_one(
            TripAssignment.trip_id == str(trip.id),
            TripAssignment.transaction_id == transaction_id,
        )
        if existing is not None:
            await existing.delete()
        return {
            "cleared": existing is not None,
            "summary": await TripService.summary(trip_id),
        }

    @staticmethod
    async def assign_many(trip_id: str, transaction_ids: list[str]) -> int:
        """
        Assign exactly the transactions the user selected.

        The ids come from the checkboxes rather than being re-derived here from
        the detector's own suggestions. That distinction is the whole point:
        the previous version assigned everything the *detector* had proposed
        and called them "ticked", which meant working down the list and then
        pressing the button swept in every row that had not been answered.
        What the page shows selected and what this writes are now the same set.

        Returns:
            How many assignments were written.
        """
        trip = await TripService._get(trip_id)
        written = 0
        for transaction_id in transaction_ids:
            await TripService._write(trip, transaction_id, TRIP_INCLUDED)
            written += 1
        logger.info("Trip %s: assigned %s selected", trip.name, written)
        return written

    @staticmethod
    async def summary(trip_id: str) -> dict:
        """Just the totals for one trip, for a page updating in place."""
        trip = await TripService._get(trip_id)
        start, end = _trip_window(trip)
        transactions = await Transaction.find_all().to_list()
        by_id = {str(t.id): t for t in transactions}
        rows = await TripService._assigned_rows(trip, by_id)
        summary = summarize(rows, start, end, trip.split_ways)
        summary["budget"] = trip.budget
        summary["budget_share"] = (
            round(summary["net"] / trip.budget, 4) if trip.budget else None
        )
        return summary

    @staticmethod
    async def search(trip_id: str, query: str) -> list[dict]:
        """
        Transactions matching a search, for attaching what the windows missed.

        The suggester is deliberately narrow — a year of everything before
        departure would bury the four lines that matter — so there has to be a
        way to reach a transaction it never offered. Matching is on the
        merchant and on the bank's own booking line, because the merchant is
        derived and the line is what was actually recorded.
        """
        trip = await TripService._get(trip_id)
        needle = (query or "").strip().lower()
        if len(needle) < 2:
            return []

        claims = await TripService._claims(exclude_trip_id=None)
        trip_names = {str(t.id): t.name for t in await Trip.find_all().to_list()}

        def hit(transaction) -> bool:
            return (
                needle in (transaction.booking_text or "").lower()
                or needle in (merchant_of(transaction) or "").lower()
            )

        matches: list = []
        page = 0
        while len(matches) < TRIP_SEARCH_RESULTS:
            batch = await (
                Transaction.find()
                .sort("-date")
                .skip(page * TRIP_SEARCH_PAGE)
                .limit(TRIP_SEARCH_PAGE)
                .to_list()
            )
            if not batch:
                break
            matches.extend(t for t in batch if hit(t))
            page += 1

        start, end = _trip_window(trip)
        results = []
        for transaction in matches[:TRIP_SEARCH_RESULTS]:
            described = _describe_transaction(transaction)
            holder = claims.get(described["id"])
            described["phase"] = assigned_phase(transaction.date, start, end)
            described["claimed_by"] = (
                None if holder == str(trip.id) else trip_names.get(holder)
            )
            described["assigned"] = holder == str(trip.id)
            results.append(described)
        return results

    @staticmethod
    async def proposals() -> dict:
        """
        Stretches of history that look like journeys and are not recorded yet.

        Recomputed on every load rather than stored: a new import can turn two
        clusters into one, and a proposal nobody has acted on is worth
        nothing. Only a refusal is persisted, because otherwise the same
        wrongly-flagged week is offered again after every upload.

        A proposal carries no name. The ledger knows the card stopped
        reporting from home and nothing else, so naming the trip is the user's
        half of the work — which is also what makes confirming it a decision
        rather than a formality.

        Returns:
            The proposals, newest first, each with the evidence behind it; the
            ones already refused, so a refusal can be undone; and what the
            detector cannot see, which the page has to say out loud.
        """
        transactions = await Transaction.find_all().to_list()
        trips = await Trip.find_all().to_list()
        refused = {
            row.anchor_id: row
            for row in await DismissedTripProposal.find_all().to_list()
        }

        by_id = {str(t.id): t for t in transactions}
        detected = find_trip_candidates(transactions)

        open_proposals, dismissed = [], []
        for candidate in detected:
            # A window already covered by a trip is not a proposal. Confirming
            # one is therefore all it takes to retire it — there is no second
            # piece of state saying it was accepted, and none to fall out of
            # step with the trips themselves.
            if any(
                overlaps(candidate.start, candidate.end, trip.start_date, trip.end_date)
                for trip in trips
            ):
                continue

            rows = [by_id[tid] for tid in candidate.transaction_ids if tid in by_id]
            item = {
                "anchor_id": candidate.anchor_id,
                "start_date": candidate.start.strftime("%Y-%m-%d"),
                "end_date": candidate.end.strftime("%Y-%m-%d"),
                "days": candidate.days,
                "charges": len(candidate.transaction_ids),
                "spend": candidate.spend,
                "countries": list(candidate.countries),
                # The sentinel is not a currency and must not be printed as
                # one; what it stands for is said in words instead.
                "currencies": [
                    code for code in candidate.currencies if code != CONVERTED_MARKER
                ],
                "converted": CONVERTED_MARKER in candidate.currencies,
                "strength": candidate.strength,
                # The merchants are what a reader actually recognises a trip
                # by. A country code is evidence; "SUPERMARKET BLED" is a
                # memory.
                "merchants": [
                    name
                    for name, _ in Counter(
                        merchant_of(row) for row in rows if merchant_of(row)
                    ).most_common(6)
                ],
                "transactions": [_describe_transaction(row) for row in rows],
            }
            if candidate.anchor_id in refused:
                dismissed.append(item)
            else:
                open_proposals.append(item)

        open_proposals.sort(key=lambda item: item["start_date"], reverse=True)
        dismissed.sort(key=lambda item: item["start_date"], reverse=True)

        return {
            "proposals": open_proposals,
            "dismissed": dismissed,
            "count": len(open_proposals),
            "dismissed_count": len(dismissed),
            "strong_count": sum(
                1 for item in open_proposals if item["strength"] == "strong"
            ),
            # Shown on the page because a detector that cannot see something
            # has to say so: a reader who is told "we found your trips" will
            # assume the ones missing did not happen.
            "home_country": home_country(transactions),
        }

    @staticmethod
    async def dismiss_proposal(anchor_id: str) -> bool:
        """
        Refuse one proposal, so the detector stops offering it.

        Returns:
            Whether this was a new refusal.
        """
        existing = await DismissedTripProposal.find_one(
            DismissedTripProposal.anchor_id == anchor_id
        )
        if existing is not None:
            return False

        transactions = await Transaction.find_all().to_list()
        for candidate in find_trip_candidates(transactions):
            if candidate.anchor_id != anchor_id:
                continue
            await DismissedTripProposal(
                anchor_id=anchor_id,
                start_date=candidate.start,
                end_date=candidate.end,
            ).insert()
            logger.info(
                "Trip proposal dismissed: %s to %s",
                candidate.start.date(),
                candidate.end.date(),
            )
            return True
        raise ValueError(f"No detected trip for {anchor_id!r}")

    @staticmethod
    async def restore_proposal(anchor_id: str) -> bool:
        """Hand one refused proposal back to the detector."""
        existing = await DismissedTripProposal.find_one(
            DismissedTripProposal.anchor_id == anchor_id
        )
        if existing is None:
            return False
        await existing.delete()
        return True

    @staticmethod
    async def set_split(trip_id: str, ways: int) -> dict:
        """
        Say how many people the whole journey was shared between.

        One number for the trip rather than a rewrite of every assignment.
        Splitting a trip is a fact about the trip, and applying it by setting
        52 shares would overwrite the ones set by hand and leave nothing to
        undo it with. Stored here, it composes with those shares instead of
        fighting them, and going back to the whole amount is one click.

        Args:
            trip_id: The trip being split.
            ways: How many people shared it. 1 puts the whole total back.

        Returns:
            The trip's recomputed totals.

        Raises:
            ValueError: Unknown trip, or a split that is not a whole number of
                people between 1 and :data:`MAX_SPLIT_WAYS`.
        """
        if not 1 <= ways <= MAX_SPLIT_WAYS:
            raise ValueError(
                f"A trip can be split between 1 and {MAX_SPLIT_WAYS} people"
            )
        trip = await TripService._get(trip_id)
        trip.split_ways = ways
        await trip.save()
        logger.info("Trip %s split %s ways", trip.name, ways)
        return {"split_ways": ways, "summary": await TripService.summary(trip_id)}
