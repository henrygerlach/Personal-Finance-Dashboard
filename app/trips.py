"""
What one journey actually cost.

The dates of a holiday do not contain its spending, and no filter over them
can. Flights and a hotel are paid for months ahead; a card payment abroad
reaches the bank a week after the flight home; a friend settles their half of
the house in October for a week in July. Reading a trip's cost off the travel
dates is therefore wrong in both directions at once — it counts the rent
debited while the flat stood empty, and misses the flights entirely.

So membership is *explicit*. A transaction belongs to a trip because somebody
said so, and the job of this module is only to propose the ones worth saying
it about. Dates drive a suggester, never the total.

**The three windows.** Around a trip from ``start`` to ``end``:

*The booking lead-in*, up to :data:`BOOKING_LEAD_DAYS` before departure. Only
the categories a trip is actually bought out of ahead of time — flights,
accommodation, package tours, a hire car. Everything else bought in the year
before a holiday is ordinary life, and offering it would bury the four lines
that matter under a thousand that do not.

The trap here is the *previous* holiday. Travel categories hold two very
different things: a booking made from home, and a fortnight of hotels and
minibuses paid for on the ground. Both are ``Travel``, and a naive lead-in
proposes last autumn's entire trip as this summer's bookings — on real data,
twenty-five ticked lines, none of them right. So a lead-in charge is only
*ticked* when it looks like a booking rather than like somebody's holiday:
:data:`BOOKING_MIN_AMOUNT` or more, and within :data:`BOOKING_TICK_DAYS`.
The rest are still listed, because the alternative is hiding a real one.

*The trip itself*. Everything charged between the two dates, minus two kinds
of line that keep running while nobody is home: payments the recurrence
detector has already recognised as a schedule, and transfers, which move
money without spending it.

*The settlement tail*, up to :data:`SETTLEMENT_TAIL_DAYS` after the return.
The bank books a card payment days after it is made — the PayPal ledger works
to the same lag — so a charge from a merchant seen during the trip and
*nowhere else in the history* is almost certainly the trip reaching the
account late. Both halves of that are needed. The supermarket at the end of
your own street is "seen during the trip" too, on the morning before the
flight, and matching on that alone tickets a fortnight of ordinary shopping
as Swedish holiday spending. A merchant that appears 213 times across the
history says nothing about where anybody was; one that appears exactly once
says everything.

**Cash is the trip's cost.** :func:`summarize` counts an assigned cash
withdrawal as spending even though the dashboard's expense totals exclude it.
Money taken from a machine abroad is spent on the trip; what it turned into
never reaches the bank, so the withdrawal is the only record there will ever
be of it.

**Both directions are kept apart.** A refund from an airline and a friend's
half of the villa are not negative spending, they are money back — reported
next to the gross rather than quietly netted into it, so the trip can answer
"what did it cost" and "what did it cost *me*" separately.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.categories import (
    MAJOR_TRAVEL,
    MINOR_CASH_WITHDRAWAL,
    MINOR_PERSON_TO_PERSON,
    minor_categories,
)
from app.normalization import card_country, foreign_currency, merchant_key, merchant_of
from app.utils import should_exclude_transaction

#: How far before departure a booking may sit and still be proposed. A year,
#: because that is the outer edge of when flights go on sale; the suggester
#: only reaches back this far for ``Travel`` charges, so the width costs
#: nothing in noise.
BOOKING_LEAD_DAYS = 365

#: The categories a trip is bought out of in advance. Derived from the
#: hierarchy rather than listed, so a new travel category is covered without
#: an edit here. "Other Travel" is deliberately left out: it is where
#: on-the-ground travel spending lands, and that is what the lead-in must not
#: mistake for a booking.
BOOKING_MINORS = frozenset(minor_categories(MAJOR_TRAVEL)) - {"Other Travel"}

#: Below this, a travel charge in the lead-in is listed but not ticked. A
#: booking for a holiday next summer is a substantial payment; a few euros in
#: a travel category a year earlier is somebody's bus fare on a different
#: trip.
BOOKING_MIN_AMOUNT = 50.0

#: Beyond this many days before departure, a booking is listed but not
#: ticked. Bookings do get made a year out, but at that distance the window is
#: mostly full of *last* year's holiday, and a ticked wrong answer costs more
#: than an unticked right one.
BOOKING_TICK_DAYS = 180

#: How long after the return a charge may land and still be proposed. Three
#: weeks covers the ordinary card-settlement lag with room for a hotel that
#: bills after checkout; anything later is a bill, not a settlement.
SETTLEMENT_TAIL_DAYS = 21

#: How late a charge can be and still be read as the trip settling rather
#: than as ordinary life resuming. The bank books a PayPal settlement one to
#: eight days later (see app.paypal.ledger), and a card payment abroad behaves
#: the same way. Past this the tail is still searched, but nothing in it is
#: ticked on the strength of timing alone.
SETTLEMENT_LAG_DAYS = 8

#: Which of the three windows a transaction falls in.
PHASE_BOOKED = "booked"
PHASE_DURING = "during"
PHASE_SETTLED = "settled"

PHASES = (PHASE_BOOKED, PHASE_DURING, PHASE_SETTLED)

#: How good the case for a candidate is. Strong and likely are ticked when the
#: review list is drawn; weak ones are listed, unticked, so that nothing in
#: the window is silently hidden.
STRENGTH_STRONG = "strong"
STRENGTH_LIKELY = "likely"
STRENGTH_WEAK = "weak"


@dataclass(frozen=True)
class Candidate:
    """
    One transaction the suggester thinks is worth a decision.

    A candidate is a proposal and nothing more: it carries no total and
    changes nothing until it is assigned.
    """

    transaction_id: str
    phase: str
    strength: str
    reason: str
    suggested: bool


def window_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """
    The half-open range the suggester reads, lead-in and tail included.

    Args:
        start: First day of the trip.
        end: Last day of the trip.

    Returns:
        The earliest datetime a candidate may carry, and the first one it may
        not. The upper bound is the day after the tail's last day, so a
        transaction stored at any time of day on that last day is inside it.
    """
    return (
        start - timedelta(days=BOOKING_LEAD_DAYS),
        end + timedelta(days=SETTLEMENT_TAIL_DAYS + 1),
    )


def assigned_phase(when: datetime, start: datetime, end: datetime) -> str:
    """
    Which side of the trip a date falls on, with no distance limit.

    Every assigned transaction has an answer here, because assignment is by
    hand and reaches wherever it needs to: a flight booked fourteen months
    ahead is still a booking, and a friend settling up in October is still a
    late settlement. Only the *suggester* is bounded — see :func:`phase_for`.

    The trip's own days are inclusive at both ends, so a transaction stored at
    any time on the last day is still during the trip rather than after it.
    """
    if when < start:
        return PHASE_BOOKED
    if when < end + timedelta(days=1):
        return PHASE_DURING
    return PHASE_SETTLED


def phase_for(when: datetime, start: datetime, end: datetime) -> str | None:
    """
    Which window a date falls in for the suggester, or ``None`` if none.

    The bounded form of :func:`assigned_phase`: the lead-in reaches back
    :data:`BOOKING_LEAD_DAYS` and the tail forward :data:`SETTLEMENT_TAIL_DAYS`,
    because a suggester with no horizon would propose the whole ledger.
    """
    phase = assigned_phase(when, start, end)
    if phase == PHASE_BOOKED and when < start - timedelta(days=BOOKING_LEAD_DAYS):
        return None
    if phase == PHASE_SETTLED and when >= end + timedelta(
        days=SETTLEMENT_TAIL_DAYS + 1
    ):
        return None
    return phase


def is_travel(transaction) -> bool:
    """Whether this is one of the categories a trip is booked out of."""
    return getattr(transaction, "major_category", None) == MAJOR_TRAVEL


#: The transfer categories that are real money on a trip.
#:
#: Transfers are kept out of spending everywhere else in the app, and for
#: savings plans, investments and money moved between your own accounts that
#: stays right here — none of it is what a holiday cost. These two are
#: different, and for the same underlying reason: the ledger cannot see what
#: the money turned into, so the transfer is the only record of the spending
#: there will ever be.
#:
#: *Cash* taken out abroad buys a fortnight of meals and taxis that never
#: reach the bank at all.
#:
#: *Money sent to a person* is how a shared trip is actually paid for. One
#: person books the house and everybody sends them their part; whoever did the
#: booking sees the whole cost and a pile of money back, and everybody else
#: sees nothing but a transfer. Leaving these out reports the second group as
#: having travelled for free.
TRIP_MONEY_TRANSFERS = frozenset({MINOR_CASH_WITHDRAWAL, MINOR_PERSON_TO_PERSON})


def is_cash_withdrawal(transaction) -> bool:
    """Whether this is cash taken out — spending on a trip, wherever it is filed."""
    return getattr(transaction, "minor_category", None) == MINOR_CASH_WITHDRAWAL


def is_person_to_person(transaction) -> bool:
    """Whether this is money moved between two people rather than to a merchant."""
    return getattr(transaction, "minor_category", None) == MINOR_PERSON_TO_PERSON


def counts_as_spending(transaction) -> bool:
    """
    Whether an assigned transaction belongs in the trip's totals.

    Everything that is not a transfer counts, as it does everywhere else. Of
    the transfers, only :data:`TRIP_MONEY_TRANSFERS` do — see that constant
    for why those two are not an exception to the rule so much as the rule
    applied honestly.

    Direction is not decided here. An outgoing person-to-person payment is
    what the trip cost and an incoming one is money back, and
    :func:`summarize` reads that off the sign rather than needing to be told.
    """
    if getattr(transaction, "minor_category", None) in TRIP_MONEY_TRANSFERS:
        return True
    return not should_exclude_transaction(transaction)


def _during_candidate(transaction) -> Candidate:
    """The case for a transaction charged between the two dates."""
    tid = str(transaction.id)
    if getattr(transaction, "is_recurring", False):
        return Candidate(
            tid,
            PHASE_DURING,
            STRENGTH_WEAK,
            "repeats on a schedule, so it was charged whether or not you travelled",
            suggested=False,
        )
    if is_cash_withdrawal(transaction):
        return Candidate(
            tid,
            PHASE_DURING,
            STRENGTH_STRONG,
            "cash taken out during the trip",
            suggested=True,
        )
    if is_person_to_person(transaction):
        return Candidate(
            tid,
            PHASE_DURING,
            STRENGTH_LIKELY,
            "between people, while you were away",
            suggested=True,
        )
    if should_exclude_transaction(transaction):
        return Candidate(
            tid,
            PHASE_DURING,
            STRENGTH_WEAK,
            "moves money without spending it",
            suggested=False,
        )
    if is_travel(transaction):
        return Candidate(
            tid,
            PHASE_DURING,
            STRENGTH_STRONG,
            "a travel charge during the trip",
            suggested=True,
        )
    return Candidate(
        tid,
        PHASE_DURING,
        STRENGTH_LIKELY,
        "charged while you were away",
        suggested=True,
    )


def _booked_candidate(transaction, start: datetime) -> Candidate | None:
    """
    The case for a transaction charged before departure.

    Only the categories a trip is bought out of ahead of time, and only ticked
    when the charge looks like a booking rather than like the previous
    holiday: big enough to be one, and close enough to departure that the
    window is not mostly somebody else's trip.

    A small or distant travel charge is still listed. The lead-in exists to
    catch the flight bought in March for August, and refusing to show a
    plausible one because it is unusual would defeat the window entirely.
    """
    if transaction.minor_category not in BOOKING_MINORS:
        return None

    days = (start - transaction.date).days
    when = f"{days} {'day' if days == 1 else 'days'} before departure"

    if abs(transaction.amount) >= BOOKING_MIN_AMOUNT and days <= BOOKING_TICK_DAYS:
        return Candidate(
            str(transaction.id),
            PHASE_BOOKED,
            STRENGTH_STRONG,
            f"a booking {when}",
            suggested=True,
        )
    return Candidate(
        str(transaction.id),
        PHASE_BOOKED,
        STRENGTH_WEAK,
        f"a travel charge {when} — as likely to be an earlier trip as a booking",
        suggested=False,
    )


def _settled_candidate(
    transaction, only_on_trip: set[str], end: datetime
) -> Candidate | None:
    """
    The case for a transaction charged after the return.

    The strong signal is a merchant seen during the trip *and nowhere else in
    the history*: the bank books a card payment days after it is made, so a
    shop that exists only inside these dates appearing a few days later is
    that visit reaching the account rather than a second one.

    Both halves are load-bearing. The supermarket at the end of your own
    street is also "seen during the trip", on the morning before the flight,
    and on real data matching on that alone ticked a fortnight of ordinary
    shopping as Swedish holiday spending.

    Args:
        transaction: The transaction to judge.
        only_on_trip: Merchant keys that occur during the trip and at no other
            point in the history.
        end: The trip's last day, to measure the settlement lag from.
    """
    tid = str(transaction.id)
    if getattr(transaction, "is_recurring", False):
        return None
    if is_cash_withdrawal(transaction):
        # Cash taken out after the return was not spent on the trip; it is
        # simply the next week's cash.
        return None
    if is_person_to_person(transaction):
        # Settling up is the one thing that genuinely arrives weeks late, and
        # it is why this feature exists at all — so it is offered for the
        # whole tail rather than only inside the settlement lag. Never ticked:
        # the ledger cannot tell a friend paying you back for the villa from
        # one paying you back for a concert.
        return Candidate(
            tid,
            PHASE_SETTLED,
            STRENGTH_WEAK,
            "money between people after the trip — this is where settling up lands",
            suggested=False,
        )
    if should_exclude_transaction(transaction):
        return None

    lag = (transaction.date - end).days
    settling = lag <= SETTLEMENT_LAG_DAYS

    if settling and merchant_key(merchant_of(transaction)) in only_on_trip:
        return Candidate(
            tid,
            PHASE_SETTLED,
            STRENGTH_STRONG,
            "seen only on this trip, booked late",
            suggested=True,
        )
    if settling and transaction.minor_category in BOOKING_MINORS:
        return Candidate(
            tid,
            PHASE_SETTLED,
            STRENGTH_STRONG,
            "travel, landed just after the return",
            suggested=True,
        )
    return Candidate(
        tid,
        PHASE_SETTLED,
        STRENGTH_WEAK,
        "landed in the settlement window, but nothing else connects it",
        suggested=False,
    )


def find_candidates(transactions, start: datetime, end: datetime) -> list[Candidate]:
    """
    Every transaction worth a decision for one trip, in date order.

    Args:
        transactions: The transactions to consider. Anything outside the three
            windows is ignored, so passing the whole history is fine.
        start: First day of the trip.
        end: Last day of the trip.

    Returns:
        One candidate per transaction the suggester will offer, oldest first.
        A candidate is a proposal: ``suggested`` says whether the review list
        should arrive with it ticked.
    """
    rows = sorted(
        (t for t in transactions if phase_for(t.date, start, end)),
        key=lambda t: t.date,
    )

    # The merchants that exist only inside the trip, which is what makes a
    # late settlement recognisable. Computed over the *whole* history rather
    # than over the window, because that is the entire point: a merchant
    # appearing 213 times across four years says nothing about where anybody
    # was, and one appearing exactly once says everything.
    #
    # Built before the tail is examined so the answer cannot depend on the
    # order the history happened to arrive in.
    #
    # The tail is excluded from "elsewhere" but the lead-in is not, and the
    # asymmetry is deliberate. A hotel that bills after checkout occurs during
    # the trip and again in the tail, and counting its own late booking
    # against it would disqualify exactly the case this set exists to catch.
    # The lead-in is a year long, and a year of ordinary shopping is precisely
    # the evidence that a merchant is not trip-specific.
    first_day, day_after = start, end + timedelta(days=1)
    tail_ends = day_after + timedelta(days=SETTLEMENT_TAIL_DAYS)
    during, elsewhere = set(), set()
    for transaction in transactions:
        key = merchant_key(merchant_of(transaction))
        if not key:
            continue
        if first_day <= transaction.date < day_after:
            during.add(key)
        elif not day_after <= transaction.date < tail_ends:
            elsewhere.add(key)
    only_on_trip = during - elsewhere

    candidates = []
    for transaction in rows:
        phase = phase_for(transaction.date, start, end)
        if phase == PHASE_DURING:
            candidate = _during_candidate(transaction)
        elif phase == PHASE_BOOKED:
            candidate = _booked_candidate(transaction, start)
        else:
            candidate = _settled_candidate(transaction, only_on_trip, end)
        if candidate:
            candidates.append(candidate)
    return candidates


def summarize(entries, start: datetime, end: datetime, split_ways: int = 1) -> dict:
    """
    What a trip cost, from the transactions assigned to it.

    Spending and money back are reported separately rather than summed into
    one figure. A refund from an airline and a friend's share of the house are
    not negative spending: the gross is what the trip cost, the net is what it
    cost *you*, and a trip where both numbers are large is a different trip
    from one where both are small.

    **The two kinds of split, and why they multiply.** ``split_ways`` says the
    whole journey was shared — two people who split everything down the middle
    — while an entry's own share says one *payment* covered somebody who is
    not on this trip's books at all. They answer different questions and
    compose: a booking where you already only owned half, on a trip you then
    split with a partner, leaves you a quarter. That is why the trip-level
    split is a separate number rather than something written onto every
    assignment: applying it by rewriting shares would collide with the ones
    set by hand, and could not be undone afterwards.

    Args:
        entries: ``(transaction, share)`` pairs, where the share is the
            fraction of the amount this trip owns — 0.5 for a booking split
            with somebody who is not on this trip's books.
        start: First day of the trip.
        end: Last day of the trip.
        split_ways: How many people the whole trip was shared between. 1 means
            it was not shared, and is the only value that changes nothing.

    Returns:
        Every figure as *your* share of it, alongside ``full_spend`` and
        ``full_net`` for what the trip cost in total. Both are reported
        because both are true and a page that showed only one of them would
        be lying by omission.
    """
    spend = 0.0
    refunds = 0.0
    cash = 0.0
    person_out = 0.0
    person_in = 0.0
    excluded = 0.0
    by_category: dict[str, dict] = {}
    by_phase = {phase: 0.0 for phase in PHASES}

    for transaction, share in entries:
        amount = transaction.amount * share
        if not counts_as_spending(transaction):
            # Assigned by hand and deliberately kept out of the totals: a
            # savings plan or a credit-card settlement that happened to fall
            # mid-trip is not what the trip cost.
            excluded += abs(amount)
            continue

        phase = assigned_phase(transaction.date, start, end)
        by_phase[phase] += -amount

        if amount < 0:
            spend += -amount
            if is_cash_withdrawal(transaction):
                cash += -amount
            elif is_person_to_person(transaction):
                person_out += -amount
            minor = transaction.minor_category or "Uncategorized"
            bucket = by_category.setdefault(
                minor,
                {
                    "minor": minor,
                    "major": transaction.major_category or "Other",
                    "amount": 0.0,
                    "count": 0,
                },
            )
            bucket["amount"] += -amount
            bucket["count"] += 1
        else:
            refunds += amount
            if is_person_to_person(transaction):
                person_in += amount

    # The trip-level split scales every figure identically, so it cannot
    # change the *shape* of the breakdown — a category's percentage of the
    # trip is the same whoever paid for it. Applied here at the end rather
    # than inside the loop so the shares stay exact and only round once.
    ways = max(1, int(split_ways or 1))
    mine = 1 / ways

    categories = sorted(by_category.values(), key=lambda row: -row["amount"])
    for row in categories:
        # The percentage is computed against the undivided total on purpose:
        # it is a proportion, and dividing both sides would only introduce
        # rounding.
        row["share"] = round(row["amount"] / spend, 4) if spend else 0.0
        row["amount"] = round(row["amount"] * mine, 2)

    days = max((end - start).days + 1, 1)
    net = spend - refunds

    return {
        "spend": round(spend * mine, 2),
        "refunds": round(refunds * mine, 2),
        "net": round(net * mine, 2),
        "cash": round(cash * mine, 2),
        # Told apart from the rest so the page can explain why two kinds of
        # transfer are in a total that excludes transfers everywhere else.
        "person_out": round(person_out * mine, 2),
        "person_in": round(person_in * mine, 2),
        "excluded": round(excluded * mine, 2),
        "count": len(entries),
        "days": days,
        "per_day": round(net * mine / days, 2),
        "categories": categories,
        "by_phase": {
            phase: round(value * mine, 2) for phase, value in by_phase.items()
        },
        "split_ways": ways,
        # What the journey cost altogether, which stays worth knowing even
        # when only a share of it was yours.
        "full_spend": round(spend, 2),
        "full_net": round(net, 2),
    }


# --- Finding the trips nobody has recorded yet -------------------------------
#
# Everything above answers "what did this trip cost" once a trip exists. This
# answers the question before it: which weeks of the history were a trip at all.
#
# The evidence is that a card reports where it was used — see
# :func:`app.normalization.card_country` and
# :func:`app.normalization.foreign_currency`. Neither is read as a country
# name; what counts is only that a charge disagrees with the code the card
# normally reports, and that code is read out of the data rather than assumed,
# because it is wherever the cardholder happens to live.
#
# On its own that marks far too much. A single foreign charge is usually an
# online purchase from a shop that happens to bill from abroad, and in the
# sample data those are the majority of foreign codes — eleven charges under
# one code spread across eleven months. What separates a journey from a
# shopping habit is that a journey is *dense*: several charges, close together,
# over more than one day. So the marked charges are clustered by date and a
# cluster has to earn its place.
#
# What this cannot see is a holiday spent inside your own country, or one paid
# for entirely in cash. There is no signal in the ledger for either, and the
# page says so rather than leaving the gap to be discovered.

#: How long a break between two foreign charges ends a trip. Set from the
#: sample data, where a six-day stretch paid in cash split one four-week
#: journey into two proposals. The cost of the wider gap is that a trip's
#: window can be dragged out by an unrelated charge a week after the return —
#: which is a date the user corrects on the way in, where a missing half of a
#: trip is not.
TRIP_GAP_DAYS = 7

#: How many foreign charges a cluster needs. Two, deliberately: a false
#: proposal costs one click to dismiss, while a trip that is never proposed is
#: never noticed at all. Recall is worth more than precision here because
#: every proposal is confirmed by hand before it becomes anything. Two charges
#: is all a week in Sweden left in the sample data.
MIN_TRIP_CHARGES = 2

#: How many distinct days a cluster must span. A single day of foreign charges
#: is a shopping session, whoever billed it.
MIN_TRIP_DAYS = 2

#: Charges above which a cluster is dense enough to be obvious. Below it a
#: proposal is still shown, but as one worth a second look rather than a
#: foregone conclusion.
STRONG_TRIP_CHARGES = 5


@dataclass(frozen=True)
class DetectedTrip:
    """
    A stretch of history that looks like a journey.

    A proposal and nothing more. It carries no name, because the ledger does
    not know where anybody went — only that the card stopped reporting from
    home. Confirming one is what creates a trip, and that is the user's to do.
    """

    #: The first foreign charge in the cluster. Used to recognise this same
    #: proposal on a later run, so dismissing it can stick.
    anchor_id: str
    start: datetime
    end: datetime
    transaction_ids: tuple
    #: The country codes seen, most frequent first. Comdirect's own numbering,
    #: shown as evidence rather than translated into a place.
    countries: tuple
    #: Currencies charged in, which is the part a reader recognises.
    currencies: tuple
    #: What was charged inside the window, as a positive amount.
    spend: float
    #: How much there is to go on. Graded rather than filtered, so a thin
    #: proposal is listed quietly instead of being dropped: two charges is
    #: what a week away paid mostly in cash leaves behind, and it is also what
    #: an online order and its refund leave behind.
    strength: str

    @property
    def days(self) -> int:
        """Length of the window, counting both end days."""
        return (self.end - self.start).days + 1


def home_country(transactions) -> str | None:
    """
    The country code the card normally reports.

    Read from the data rather than assumed: it is wherever the cardholder
    lives, and this app is not written for one country. Returns None when
    there are no card lines to judge from, which switches the country half of
    the evidence off rather than guessing.
    """
    codes = Counter(
        code
        for transaction in transactions
        if (code := card_country(transaction.booking_text))
    )
    most_common = codes.most_common(1)
    return most_common[0][0] if most_common else None


def _abroad_marks(transaction, home: str | None) -> tuple[str | None, str | None]:
    """
    What says this charge was made away from home, if anything.

    Returns:
        The country code and the currency that make the case, either of which
        may be None. Both None means the charge is no evidence at all.
    """
    currency = foreign_currency(transaction.booking_text)
    code = card_country(transaction.booking_text)
    if code is not None and (home is None or code == home):
        code = None
    return code, currency


def find_trip_candidates(transactions) -> list[DetectedTrip]:
    """
    The stretches of history that look like journeys, oldest first.

    Args:
        transactions: The whole history. The home country code is read from
            it, so a narrow slice would give a worse answer than none.

    Returns:
        One proposal per cluster that clears :data:`MIN_TRIP_CHARGES` and
        :data:`MIN_TRIP_DAYS`. Every one still has to be confirmed: the ledger
        can say the card was somewhere else, and nothing more than that.
    """
    home = home_country(transactions)

    marked = []
    for transaction in transactions:
        code, currency = _abroad_marks(transaction, home)
        if code or currency:
            marked.append((transaction, code, currency))
    marked.sort(key=lambda row: row[0].date)

    # Cut the marked charges into runs wherever the card goes quiet for longer
    # than a trip would allow.
    clusters, current = [], []
    for row in marked:
        if current and (row[0].date - current[-1][0].date).days > TRIP_GAP_DAYS:
            clusters.append(current)
            current = []
        current.append(row)
    if current:
        clusters.append(current)

    detected = []
    for cluster in clusters:
        rows = [row[0] for row in cluster]
        days = {row.date.date() for row in rows}
        if len(rows) < MIN_TRIP_CHARGES or len(days) < MIN_TRIP_DAYS:
            continue

        countries = Counter(row[1] for row in cluster if row[1])
        currencies = Counter(row[2] for row in cluster if row[2])
        detected.append(
            DetectedTrip(
                anchor_id=str(rows[0].id),
                start=rows[0].date,
                end=rows[-1].date,
                transaction_ids=tuple(str(row.id) for row in rows),
                countries=tuple(code for code, _ in countries.most_common()),
                currencies=tuple(code for code, _ in currencies.most_common()),
                spend=round(sum(-row.amount for row in rows if row.amount < 0), 2),
                strength=(
                    STRENGTH_STRONG
                    if len(rows) >= STRONG_TRIP_CHARGES
                    else STRENGTH_LIKELY
                ),
            )
        )
    return detected


def overlaps(start: datetime, end: datetime, other_start, other_end) -> bool:
    """Whether two inclusive date windows share any day."""
    return start <= other_end and other_start <= end
