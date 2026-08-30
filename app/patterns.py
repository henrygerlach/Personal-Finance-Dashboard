"""
What changed in the spending, and what stayed the same.

The dashboard already draws every month next to every other. What it cannot
do is say which of those differences *mean* something: a bar is taller, and
the eye has no way of knowing whether that is a holiday, a price rise, a new
habit, or the ordinary noise of a month with five weekends in it. This module
is the part that decides.

Everything here is a *finding about a series*, and three rules hold across all
of them.

**Robust statistics, never means.** Fifty-five months of a real ledger contain
several holidays, a deposit and a tax bill, and each of those moves a mean far
enough to hide everything else. Levels are medians and spread is the median
absolute deviation, so one extraordinary month changes a threshold by nothing
at all — which is exactly what lets that month be *reported* as extraordinary
instead of quietly raising the bar for its neighbours.

**Every threshold is a share and a floor, and the floor is relative.** A rule
that fires above thirty euros is a rule written for one person's ledger. Every
absolute test here is a fraction of that ledger's own median monthly spending,
so the same constants behave the same way for somebody spending ten times as
much or a tenth as much.

**The trailing month is a lie until the statement lands.** Statements arrive
in batches, roughly monthly, so the newest month in the database is usually a
few days of one. Comparing it to anything reports a catastrophic collapse in
spending every time, so :func:`complete_months` drops it — and the first month
too, which is as likely to start mid-statement.

Nothing here reads the database and nothing here decides anything on the
user's behalf: a finding is a proposal, carrying the transactions that prove
it so the claim can be opened and checked in one click. What the user does
with it is a :class:`app.models.PatternDismissal`.
"""

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import median
from typing import Iterable, Sequence

from app.categories import (
    MAJOR_BY_MINOR,
    MINOR_CASH_WITHDRAWAL,
    flow_type_for,
    is_valid_major,
)
from app.normalization import (
    build_canonical_map,
    merchant_key,
    merchant_of,
    normalize_merchant,
)
from app.recurrence import PERIOD_MONTHS
from app.utils import should_exclude_transaction

#: The kinds of finding. Each is a different question asked of the history,
#: and the page renders one card shape per kind, so the strings are part of
#: the contract with the frontend rather than a private label.
KIND_DRIFT = "drift"  # a category settled at a new level
KIND_OUTLIER = "outlier"  # one month unlike the others
KIND_PRICE = "price"  # a schedule changed what it charges
KIND_LAPSED = "lapsed"  # a schedule stopped collecting
KIND_COMMITMENT = "commitment"  # the standing monthly floor moved
KIND_SEASONAL = "seasonal"  # one calendar month is reliably different
KIND_GONE = "gone"  # a regular merchant went quiet
KIND_ARRIVED = "arrived"  # a new merchant became a habit
KIND_MIX = "mix"  # the composition moved while the total held
KIND_SAVINGS = "savings"  # the share of income kept changed
KIND_CASH = "cash"  # cash withdrawal habits changed

#: Every kind there is, so a dismissal that could never match a finding is
#: refused when it is written rather than sitting in the collection forever.
KINDS = frozenset(
    {
        KIND_DRIFT,
        KIND_OUTLIER,
        KIND_PRICE,
        KIND_LAPSED,
        KIND_COMMITMENT,
        KIND_SEASONAL,
        KIND_GONE,
        KIND_ARRIVED,
        KIND_MIX,
        KIND_SAVINGS,
        KIND_CASH,
    }
)

#: How many months make up "now". Three, because one month is noise and six
#: is slow enough that a change is old news by the time it is reported.
RECENT_MONTHS = 3

#: How many months before that make up "normally". A year, so the comparison
#: is against every season rather than against last autumn.
BASELINE_MONTHS = 12

#: Below this many months of baseline there is nothing to compare against.
#: Half a year is the point where a median stops being one number's opinion.
MIN_BASELINE_MONTHS = 6

#: How far from a month's edge the history may stop or start and still leave
#: that month usable. A statement rarely ends exactly on the 31st, and no
#: ledger has a transaction on every one of its final days.
MONTH_END_SLACK_DAYS = 4

#: The materiality floor, as a share of median monthly spending. Below this a
#: finding is arithmetically real and not worth a card: "pet food up 140%"
#: on a ledger where pet food is four euros is noise wearing a percentage.
FLOOR_SHARE = 0.02

#: How far a category's level must move before the move is a change of level
#: rather than the ordinary wobble of a small category.
DRIFT_MIN_SHARE = 0.25

#: …except that a large enough move is a change of level however small a
#: share of its own category it is. A share test alone protects the biggest
#: categories from ever being reported: groceries are a quarter of a ledger,
#: so a quarter of *them* is a sixteenth of everything, and a hundred euros a
#: month can move in and out of the shopping without a card ever appearing.
#: This is that escape hatch, as a share of the whole median month.
DRIFT_BIG_MOVE_SHARE = 0.08

#: When one minor category explains this much of its major's move, the finding
#: is reported about the minor. "Food & Drink up 180" is true and useless next
#: to "Delivery & Takeout up 165", which is the same fact with the answer in
#: it.
DRIFT_EXPLAINS_SHARE = 0.6

#: How many robust standard deviations from the median a month must sit to be
#: called unusual. Three is deliberately far out: this fires on a handful of
#: months in a five-year history, and a page of outliers is a page of nothing.
OUTLIER_Z = 3.0

#: …and it must also miss the median by this share of it, so a ledger with
#: unusually steady months cannot have an ordinary one flagged for being
#: twenty euros out.
OUTLIER_MIN_SHARE = 0.20

#: How far back outliers are worth reporting. Beyond two years an unusual
#: month is not news, it is history the user has already lived through.
OUTLIER_HORIZON_MONTHS = 24

#: What share of a month's excess one thing must account for before that thing
#: is named as the reason. Below half, the month was expensive in general and
#: naming a cause would be picking the largest of many.
ATTRIBUTION_SHARE = 0.5

#: How many different years must show the same calendar month before its shape
#: is a season rather than a coincidence of two Decembers.
MIN_SEASON_YEARS = 3

#: Below this much history there are no seasons to find.
MIN_SEASON_MONTHS = 24

#: How far a calendar month's median must sit from the overall median to be
#: worth saying out loud, above and below.
SEASON_HIGH = 1.35
SEASON_LOW = 0.7

#: How far ahead a season is still worth a card.
#:
#: A season is the one finding on this page that is about the future, and it
#: is only useful before it arrives: being told in March that December is
#: expensive is being told nothing that can be acted on, and a five-year
#: history has twelve such facts per category waiting to fill the page with
#: them. The profile chart shows the whole year; the cards are what is coming.
SEASON_HORIZON_MONTHS = 3

#: What share of the observed years must agree on which side of normal a
#: calendar month sits. A median already puts half of them there, so this is
#: the test that separates a season from two memorable years and three
#: ordinary ones.
SEASON_CONSISTENCY = 2 / 3

#: A season claims something about every year, so it clears a higher bar than
#: a one-off: this many times the ordinary materiality floor.
SEASON_FLOOR_MULTIPLE = 2.0

#: How many times its own typical gap a merchant must be silent before it has
#: gone quiet rather than being between visits, and the shortest silence that
#: can count at all — a merchant visited weekly is not gone after a fortnight.
GONE_QUIET_GAPS = 3.0
GONE_QUIET_MIN_DAYS = 90

#: How much of a habit a merchant must have been before its silence is worth
#: reporting, in visits and in days.
MIN_REGULAR_VISITS = 8
MIN_REGULAR_SPAN_DAYS = 180

#: What makes a newcomer a habit rather than a purchase: seen this often,
#: first seen inside this window.
ARRIVED_MIN_VISITS = 3
ARRIVED_WINDOW_MONTHS = 6

#: How still the total must be before a change in its composition is the
#: story, and how far a category's share must move to be part of that story.
MIX_STABLE_SHARE = 0.10
MIX_MIN_POINTS = 0.05

#: How far the share of income kept must move to be reported, in points.
SAVINGS_MIN_MOVE = 0.08

#: Turns a median absolute deviation into the standard deviation of a normal
#: distribution, so a threshold in "sigmas" means what it usually means.
MAD_TO_SIGMA = 1.4826

#: How many transactions a finding carries as proof. The same count the
#: dashboard drill-down uses: enough to show what drove it, not a statement.
PROOF_ROWS = 8

#: How much of a series a card's sparkline draws.
SPARK_MONTHS = 24


@dataclass(frozen=True)
class Schedule:
    """
    One standing commitment, as the patterns need it.

    Built from what :mod:`app.recurrence` detected and what the user has since
    said about it, rather than re-detected here: whether something is a
    schedule, and whether it is still running, are questions the recurring
    page has already answered — and answered with the user's overrides folded
    in, which this module has no business second-guessing.
    """

    merchant: str
    merchant_key: str
    period: str
    minor: str
    amount: float
    direction: str
    status: str
    first_seen: datetime
    last_seen: datetime
    next_expected: datetime
    occurrences: int
    transaction_ids: tuple[str, ...]
    price_change: dict | None = None

    @property
    def monthly_equivalent(self) -> float:
        """What this costs a month, whatever interval it bills on."""
        return abs(self.amount) / PERIOD_MONTHS.get(self.period, 1)


@dataclass(frozen=True)
class Pattern:
    """
    One finding, and everything needed to show it and to check it.

    ``kind``, ``subject`` and ``month`` are its identity, and they are facts
    rather than a recomputed id: the kind of question, the thing it is about,
    and the month it concerns. Findings are recomputed from scratch on every
    page load — a new statement can end one and start another — so anything
    keyed on a position in a list would stop meaning the same thing the moment
    the list changed. A dismissal is stored against that triple.
    """

    kind: str
    subject: str
    month: str
    label: str
    headline: float
    category: str = ""
    facts: dict = field(default_factory=dict)
    series: tuple[tuple[str, float], ...] = ()
    transaction_ids: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        """What identifies this finding across a recomputation."""
        return (self.kind, self.subject, self.month)


@dataclass(frozen=True)
class Analysis:
    """Everything the patterns page draws: the structure, then the findings."""

    months: tuple[str, ...]
    spend: tuple[float, ...]
    income: tuple[float, ...]
    committed: tuple[float, ...]
    fixed: tuple[float, ...]
    seasonal: tuple[dict, ...]
    findings: tuple[Pattern, ...]
    floor: float
    reference: str


# --- Months ------------------------------------------------------------------


def month_key(moment: datetime) -> str:
    """The ``YYYY-MM`` a moment falls in."""
    return moment.strftime("%Y-%m")


def _index(key: str) -> int:
    """A month key as a single number, so months can be counted and shifted."""
    year, month = key.split("-")
    return int(year) * 12 + int(month) - 1


def _key(index: int) -> str:
    """The inverse of :func:`_index`."""
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def month_range(first: str, last: str) -> list[str]:
    """Every month key from ``first`` to ``last`` inclusive."""
    return [_key(i) for i in range(_index(first), _index(last) + 1)]


def complete_months(transactions: Sequence) -> list[str]:
    """
    The months whose statements have actually arrived, in order.

    A partial month is worse than a missing one. Statements are uploaded in
    batches, so the newest month in the database is usually four days long,
    and every comparison drawn against it reports the same dramatic collapse
    in spending. The first month has the same problem from the other end: a
    ledger starting on the 14th has a half January in it.

    A month is kept when the history reaches within
    :data:`MONTH_END_SLACK_DAYS` of its far edge. That occasionally discards a
    complete month whose last few days happened to be quiet, which is the
    cheap direction to be wrong in.
    """
    dates = [t.date for t in transactions]
    if not dates:
        return []

    first, last = min(dates), max(dates)
    months = month_range(month_key(first), month_key(last))

    last_day = calendar.monthrange(last.year, last.month)[1]
    if months and last.day < last_day - MONTH_END_SLACK_DAYS:
        months.pop()
    if months and first.day > 1 + MONTH_END_SLACK_DAYS:
        months.pop(0)
    return months


def _windows(months: Sequence[str]) -> tuple[list[str], list[str]]:
    """
    "Now" and "normally", as two lists of months.

    Returns:
        The most recent months, and the year before them. Either can be short;
        callers check the baseline against :data:`MIN_BASELINE_MONTHS`.
    """
    recent = list(months[-RECENT_MONTHS:])
    start = max(0, len(months) - RECENT_MONTHS - BASELINE_MONTHS)
    baseline = list(months[start : len(months) - RECENT_MONTHS])
    return recent, baseline


# --- Statistics --------------------------------------------------------------


def _median(values: Iterable[float]) -> float:
    ordered = list(values)
    return median(ordered) if ordered else 0.0


def _spread(values: Sequence[float]) -> float:
    """
    The robust equivalent of a standard deviation.

    A standard deviation over a real ledger is mostly a description of its
    holidays: two extraordinary months in five years inflate it enough that
    nothing else can ever look unusual again. The median absolute deviation
    ignores them, which is what makes them findable.
    """
    if not values:
        return 0.0
    centre = median(values)
    return median([abs(value - centre) for value in values]) * MAD_TO_SIGMA


def _level(series: dict[str, float], months: Sequence[str]) -> float:
    """The typical value of a series over some months, zero-filling gaps."""
    return _median([series.get(month, 0.0) for month in months])


def _material(delta: float, before: float, floor: float, big: float) -> bool:
    """
    Whether a change is worth a card.

    Nothing below the floor ever is: that is what keeps a category of four
    euros off the page however violently it doubles. Above it, a change
    qualifies either by being a large share of its own category or by being
    large full stop — the second test exists because the first one silently
    exempts the biggest categories, where a quarter of the category is a
    sixteenth of the ledger and a hundred euros a month can come and go
    unreported.
    """
    if abs(delta) < floor:
        return False
    return abs(delta) >= big or abs(delta) >= DRIFT_MIN_SHARE * abs(before)


# --- Reading the ledger ------------------------------------------------------


def _flow(transaction) -> str:
    """
    Income, expense or transfer, derived rather than guessed.

    The major category decides, exactly as it does everywhere else. A
    transaction from before the taxonomy migration has no usable major, and
    falls back to the same check the charts use.
    """
    major = getattr(transaction, "major_category", None)
    if major and is_valid_major(major):
        return flow_type_for(major)
    if should_exclude_transaction(transaction):
        return "transfer"
    return "income" if transaction.amount > 0 else "expense"


def _spend_rows(transactions: Sequence) -> list:
    """Everything that counts as spending, refunds against it included."""
    return [t for t in transactions if _flow(t) == "expense"]


def _income_rows(transactions: Sequence) -> list:
    return [t for t in transactions if _flow(t) == "income"]


def _spend_of(transaction) -> float:
    """
    Spending as a positive number.

    A refund filed under an expense category comes through negative, which is
    what nets it off the category it was refunded from — the same arithmetic
    the dashboard's own totals do.
    """
    return -transaction.amount


def _sum_by_month(rows: Sequence, value=_spend_of) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for row in rows:
        totals[month_key(row.date)] += value(row)
    return dict(totals)


def _count_by_month(rows: Sequence) -> dict[str, float]:
    counts: dict[str, float] = defaultdict(float)
    for row in rows:
        counts[month_key(row.date)] += 1
    return dict(counts)


def _series_of(series: dict[str, float], months: Sequence[str]) -> tuple:
    """A series as the page draws it: the tail of it, zero-filled."""
    return tuple((month, round(series.get(month, 0.0), 2)) for month in months)


def _proof(rows: Sequence) -> tuple[str, ...]:
    """The transactions behind a finding, largest first."""
    ordered = sorted(rows, key=lambda t: (-abs(t.amount), t.date))
    return tuple(str(t.id) for t in ordered[:PROOF_ROWS])


def _merchant_keys(transactions: Sequence) -> dict[str, tuple[str, str]]:
    """
    Every transaction's merchant, as a key and a readable name.

    Grouped the same way :mod:`app.recurrence` groups, through the canonical
    map, so a merchant that a line wrap split into two spellings is one
    merchant here too — otherwise a habit would look like it stopped and a
    different one started on the same day.
    """
    names = {
        str(t.id): normalize_merchant(t.booking_text, getattr(t, "process", None))
        for t in transactions
    }
    canonical = build_canonical_map(name for name in names.values() if name)
    resolved = {}
    for tid, name in names.items():
        if not name:
            continue
        display = canonical.get(name, name)
        resolved[tid] = (merchant_key(display), display)
    return resolved


# --- Category drift ----------------------------------------------------------


def _decompose(
    rows: Sequence,
    recent: Sequence[str],
    baseline: Sequence[str],
) -> dict:
    """
    Split a category's move into going more often and paying more each time.

    This is the question a bar chart cannot answer and the one worth asking.
    Sixty euros more on cafés is a different fact depending on which of the
    two moved: eleven visits instead of four at the same four-euro cup is a
    habit, and the same four visits at eleven euros is a price.

    The split is exact rather than approximate. With ``C`` visits at an
    average ``T``, spending is ``C × T``, and::

        (Ca - Cb) × Tb  +  (Ta - Tb) × Ca  =  Ca×Ta - Cb×Tb

    so the two effects add up to the whole change with nothing left over. The
    first term prices the extra visits at the old ticket; the second prices
    the ticket change across the visits actually made.

    Returns:
        The two halves of the change and the counts and tickets behind them.
    """
    spend = _sum_by_month(rows)
    counts = _count_by_month(rows)

    before, after = _level(spend, baseline), _level(spend, recent)
    count_before, count_after = _level(counts, baseline), _level(counts, recent)

    # A category with no visits in a window has no average ticket, and the
    # whole change is then the arrival or disappearance of the visits.
    ticket_before = before / count_before if count_before else 0.0
    ticket_after = after / count_after if count_after else 0.0

    return {
        "count_before": round(count_before, 2),
        "count_after": round(count_after, 2),
        "ticket_before": round(ticket_before, 2),
        "ticket_after": round(ticket_after, 2),
        "from_frequency": round((count_after - count_before) * ticket_before, 2),
        "from_ticket": round((ticket_after - ticket_before) * count_after, 2),
    }


def _drift(
    transactions: Sequence,
    months: Sequence[str],
    floor: float,
    big: float,
) -> list[Pattern]:
    """
    Categories that have settled at a new level.

    Not "this month was expensive" — that is :func:`_outliers` — but a level
    the last three months agree on and the year before them does not. The
    comparison is median against median for that reason: three months is
    already enough for one of them to be a holiday, and a mean of three would
    then report the holiday as the new normal.

    Reported about the *most specific* category that explains the move.
    Majors are tested, because that is where a change large enough to matter
    shows up, but a major's move is usually one minor's move wearing a bigger
    name, and saying "Food & Drink is up 180" next to "Delivery is up 165" is
    saying the same thing twice with the answer only in the second one.
    """
    recent, baseline = _windows(months)
    if len(baseline) < MIN_BASELINE_MONTHS:
        return []

    rows = _spend_rows(transactions)
    by_major: dict[str, list] = defaultdict(list)
    by_minor: dict[str, list] = defaultdict(list)
    for row in rows:
        by_major[row.major_category or "Other"].append(row)
        by_minor[row.minor_category or "Uncategorized"].append(row)

    window = set(recent)
    baseline_window = set(baseline)
    findings = []
    for major, major_rows in by_major.items():
        spend = _sum_by_month(major_rows)
        before, after = _level(spend, baseline), _level(spend, recent)
        delta = after - before
        if not _material(delta, before, floor, big):
            continue

        # The minor that carries the major's move, if one of them does. It has
        # to move the same way and be a real change on its own terms, so a
        # major that drifted because five minors each drifted a little is
        # still reported as the major it is.
        subject, subject_rows = major, major_rows
        for minor, minor_rows in by_minor.items():
            if MAJOR_BY_MINOR.get(minor) != major:
                continue
            minor_spend = _sum_by_month(minor_rows)
            minor_before = _level(minor_spend, baseline)
            minor_delta = _level(minor_spend, recent) - minor_before
            if minor_delta * delta <= 0:
                continue
            if abs(minor_delta) < DRIFT_EXPLAINS_SHARE * abs(delta):
                continue
            if not _material(minor_delta, minor_before, floor, big):
                continue
            subject, subject_rows = minor, minor_rows
            before = minor_before
            after = _level(minor_spend, recent)
            delta = minor_delta
            break

        spark = _sum_by_month(subject_rows)
        findings.append(
            Pattern(
                kind=KIND_DRIFT,
                subject=subject,
                month=recent[-1],
                label=subject,
                category=subject,
                headline=round(delta, 2),
                facts={
                    "before": round(before, 2),
                    "after": round(after, 2),
                    "major": major,
                    "months": len(recent),
                    "baseline_months": len(baseline),
                    **_decompose(subject_rows, recent, baseline),
                },
                series=_series_of(spark, months[-SPARK_MONTHS:]),
                # A category that stopped has nothing in the recent window,
                # and the payments that are no longer being made are the whole
                # point of the card — so the proof falls back to what it was
                # spending before.
                transaction_ids=_proof(
                    [r for r in subject_rows if month_key(r.date) in window]
                    or [r for r in subject_rows if month_key(r.date) in baseline_window]
                ),
            )
        )
    return findings


# --- Unusual months ----------------------------------------------------------


def _attribute(
    rows: Sequence,
    excess: float,
    trip_of: dict[str, str],
    scheduled: set[str],
    typical: dict[str, float],
) -> dict | None:
    """
    What made one month unlike the others, when one thing did.

    Asked in order of how much it explains, because the answers are not
    mutually exclusive and the most useful one is the most specific. A journey
    is checked first: a month containing a holiday is not an anomaly, it is a
    holiday, and the trip pages already know what it cost. Then a single
    payment, then a category, then the annual bills that happen to collide in
    one month.

    Nothing is named unless it accounts for :data:`ATTRIBUTION_SHARE` of the
    excess on its own. A month that was expensive across the board has no
    cause worth naming, and naming the largest of many would read as one.
    """
    if excess <= 0:
        return None

    trips: dict[str, float] = defaultdict(float)
    for row in rows:
        name = trip_of.get(str(row.id))
        if name:
            trips[name] += _spend_of(row)
    if trips:
        name, amount = max(trips.items(), key=lambda item: item[1])
        if amount >= ATTRIBUTION_SHARE * excess:
            return {"reason": "trip", "label": name, "amount": round(amount, 2)}

    biggest = max(rows, key=_spend_of, default=None)
    if biggest is not None and _spend_of(biggest) >= ATTRIBUTION_SHARE * excess:
        return {
            "reason": "payment",
            # The merchant, not the category it was filed under: "one payment
            # to a furniture shop" is the answer, and "Furniture & Household
            # Goods" is a restatement of the question.
            "label": merchant_of(biggest) or biggest.minor_category or "",
            "category": biggest.minor_category or "Uncategorized",
            "amount": round(_spend_of(biggest), 2),
            "transaction_id": str(biggest.id),
        }

    # A category explains a month by what it did *beyond its own normal*, not
    # by being large. Groceries are a quarter of most ledgers, so on the plain
    # amount they would explain every expensive month there has ever been —
    # which is how a month that was simply expensive across the board ends up
    # with a cause named against it.
    categories: dict[str, float] = defaultdict(float)
    for row in rows:
        categories[row.minor_category or "Uncategorized"] += _spend_of(row)
    if categories:
        name, amount = max(
            categories.items(),
            key=lambda item: item[1] - typical.get(item[0], 0.0),
        )
        beyond = amount - typical.get(name, 0.0)
        if beyond >= ATTRIBUTION_SHARE * excess:
            return {
                "reason": "category",
                "label": name,
                "amount": round(amount, 2),
                "usual": round(typical.get(name, 0.0), 2),
            }

    annual = sum(_spend_of(r) for r in rows if str(r.id) in scheduled)
    if annual >= ATTRIBUTION_SHARE * excess:
        return {"reason": "schedules", "label": "", "amount": round(annual, 2)}
    return None


def _outliers(
    transactions: Sequence,
    months: Sequence[str],
    spend: dict[str, float],
    trip_of: dict[str, str],
    schedules: Sequence[Schedule],
) -> list[Pattern]:
    """
    Months that do not belong to the same distribution as the rest.

    Measured against the median and the median absolute deviation, so the
    holidays being looked for cannot raise the bar that is supposed to catch
    them. Both directions are reported: a month that cost half of normal is as
    much a fact about the history as one that cost double, and it is usually
    the more surprising of the two.

    Every outlier is attributed before it is offered, because most of them
    have an explanation the app already holds — a trip, one large payment, a
    yearly premium — and an unexplained month is a completely different thing
    to be told about than an explained one.
    """
    values = [spend.get(month, 0.0) for month in months]
    if len(values) < MIN_BASELINE_MONTHS:
        return []

    centre = _median(values)
    spread = _spread(values)
    if not centre:
        return []

    by_month: dict[str, list] = defaultdict(list)
    for row in _spend_rows(transactions):
        by_month[month_key(row.date)].append(row)

    # Only schedules that bill less often than monthly are worth naming as a
    # cause: a monthly charge is in every month and explains no difference
    # between them.
    scheduled = {
        tid
        for schedule in schedules
        if PERIOD_MONTHS.get(schedule.period, 1) > 1
        for tid in schedule.transaction_ids
    }

    # What each category costs in an ordinary month, so a month can be
    # explained by what exceeded its normal rather than by what is merely big.
    typical: dict[str, float] = {}
    by_category: dict[str, list] = defaultdict(list)
    for rows_of_month in by_month.values():
        for row in rows_of_month:
            by_category[row.minor_category or "Uncategorized"].append(row)
    for category, category_rows in by_category.items():
        series = _sum_by_month(category_rows)
        typical[category] = _median([series.get(month, 0.0) for month in months])

    horizon = set(months[-OUTLIER_HORIZON_MONTHS:])
    findings = []
    for month in months:
        if month not in horizon:
            continue
        value = spend.get(month, 0.0)
        deviation = value - centre
        if abs(deviation) < max(OUTLIER_MIN_SHARE * centre, spread * OUTLIER_Z):
            continue

        rows = by_month.get(month, [])
        findings.append(
            Pattern(
                kind=KIND_OUTLIER,
                subject="",
                month=month,
                label=month,
                headline=round(deviation, 2),
                facts={
                    "spend": round(value, 2),
                    "typical": round(centre, 2),
                    # A ledger steady enough to have no spread at all makes
                    # the distance meaningless rather than infinite, so the
                    # share test above is the only one that spoke and the
                    # card says nothing about sigmas.
                    "sigmas": round(deviation / spread, 2) if spread else None,
                    "attribution": _attribute(
                        rows, deviation, trip_of, scheduled, typical
                    ),
                },
                series=_series_of(spend, months[-SPARK_MONTHS:]),
                transaction_ids=_proof(rows),
            )
        )
    return findings


# --- Standing commitments ----------------------------------------------------


def _price_at(schedule: Schedule, month: str) -> float:
    """
    What a schedule cost in one month, per month.

    A schedule that has been repriced held two different levels over its life,
    and carrying today's price backwards would draw a commitment floor that
    rose the day the history was re-read rather than the day the letter
    arrived.
    """
    amount = abs(schedule.amount)
    change = schedule.price_change
    if change and month < change.get("changed_on", ""):
        amount = abs(change.get("from", amount))
    return amount / PERIOD_MONTHS.get(schedule.period, 1)


def _commitment_series(
    schedules: Sequence[Schedule], months: Sequence[str]
) -> dict[str, float]:
    """
    The standing monthly floor, month by month.

    What a schedule bills is not what it costs per month — a yearly premium is
    not a December problem — so everything is carried at its monthly
    equivalent, which is the only form in which a 90 EUR insurance and a 4.99
    subscription can be added together at all.

    A schedule counts from its first charge to its last, and a still-running
    one counts to the end of the history. Only money going out: what this
    number is for is the question of how much of a month is already spoken
    for before any decision gets made.
    """
    floor: dict[str, float] = {month: 0.0 for month in months}
    for schedule in schedules:
        if schedule.direction != "out":
            continue
        start = month_key(schedule.first_seen)
        if schedule.status == "ended":
            end = month_key(schedule.last_seen)
        else:
            end = months[-1]
        for month in months:
            if start <= month <= end:
                floor[month] += _price_at(schedule, month)
    return floor


def _from_schedules(
    schedules: Sequence[Schedule],
    months: Sequence[str],
    committed: dict[str, float],
    floor: float,
) -> list[Pattern]:
    """
    What the standing commitments say: prices, silences, and the floor itself.

    All three come from the same place because they are the same subject seen
    three ways — what a schedule charges, whether it still charges, and what
    they add up to.
    """
    findings: list[Pattern] = []

    for schedule in schedules:
        change = schedule.price_change
        if change and schedule.status != "ended":
            monthly = PERIOD_MONTHS.get(schedule.period, 1)
            delta = (abs(change["to"]) - abs(change["from"])) / monthly
            findings.append(
                Pattern(
                    kind=KIND_PRICE,
                    subject=schedule.merchant_key,
                    month=change["changed_on"],
                    label=schedule.merchant,
                    category=schedule.minor,
                    headline=round(delta, 2),
                    facts={
                        "from": change["from"],
                        "to": change["to"],
                        "period": schedule.period,
                        "annual": round(delta * 12, 2),
                        "direction": schedule.direction,
                    },
                    transaction_ids=schedule.transaction_ids[-PROOF_ROWS:],
                )
            )

        # A schedule the detector says has stopped, without anybody saying so.
        # Either it was cancelled and the money is free, or a collection
        # failed and it is about to be taken twice.
        if schedule.status == "ended" and schedule.direction == "out":
            if schedule.monthly_equivalent < floor:
                continue
            findings.append(
                Pattern(
                    kind=KIND_LAPSED,
                    subject=schedule.merchant_key,
                    month=month_key(schedule.last_seen),
                    label=schedule.merchant,
                    category=schedule.minor,
                    headline=round(-schedule.monthly_equivalent, 2),
                    facts={
                        "amount": round(abs(schedule.amount), 2),
                        "period": schedule.period,
                        "last_seen": schedule.last_seen.strftime("%Y-%m-%d"),
                        "expected": schedule.next_expected.strftime("%Y-%m-%d"),
                        "occurrences": schedule.occurrences,
                    },
                    transaction_ids=schedule.transaction_ids[-PROOF_ROWS:],
                )
            )

    # The floor itself, against where it stood a year ago. This is the one
    # number on the page that is about the future rather than the past:
    # everything in it will be charged again next month without anybody
    # deciding anything.
    if len(months) > BASELINE_MONTHS:
        now = committed.get(months[-1], 0.0)
        then = committed.get(months[-BASELINE_MONTHS - 1], 0.0)
        if abs(now - then) >= floor:
            findings.append(
                Pattern(
                    kind=KIND_COMMITMENT,
                    subject="",
                    month=months[-1],
                    label="Standing commitments",
                    headline=round(now - then, 2),
                    facts={
                        "now": round(now, 2),
                        "then": round(then, 2),
                        "since": months[-BASELINE_MONTHS - 1],
                        "annual": round((now - then) * 12, 2),
                        "count": sum(1 for s in schedules if s.status != "ended"),
                    },
                    series=_series_of(committed, months[-SPARK_MONTHS:]),
                    transaction_ids=tuple(
                        schedule.transaction_ids[-1]
                        for schedule in sorted(
                            (s for s in schedules if s.status != "ended"),
                            key=lambda s: -s.monthly_equivalent,
                        )[:PROOF_ROWS]
                        if schedule.transaction_ids
                    ),
                )
            )
    return findings


# --- Seasons -----------------------------------------------------------------


def _seasonal_profile(months: Sequence[str], spend: dict[str, float]) -> list[dict]:
    """
    What each calendar month typically costs, as a share of a normal month.

    Only computable with years of history, and only honest with several
    observations of the same month: two Decembers containing one wedding
    between them are not a season. Each calendar month is its own median
    against the median of everything, so the profile says "December runs at
    1.6 normal months" rather than quoting a number that depends on which
    years happen to be in the file.
    """
    if len(months) < MIN_SEASON_MONTHS:
        return []

    overall = _median([spend.get(month, 0.0) for month in months])
    if not overall:
        return []

    profile = []
    for number in range(1, 13):
        values = [spend.get(month, 0.0) for month in months if int(month[5:]) == number]
        profile.append(
            {
                "month": number,
                "years": len(values),
                "typical": round(_median(values), 2) if values else 0.0,
                "index": round(_median(values) / overall, 3) if values else None,
                "known": len(values) >= MIN_SEASON_YEARS,
            }
        )
    return profile


def _seasons(
    transactions: Sequence,
    months: Sequence[str],
    floor: float,
) -> list[Pattern]:
    """
    Calendar months that are reliably unlike the others, and are still ahead.

    Reported per major rather than for the total, because that is where the
    fact is usable: "December costs more" is something everybody already
    knows, and "December runs at four normal months for Gifts" is a thing to
    plan around.

    Only the next few calendar months get a card. A season is the one finding
    here that is about the future, and a category with five years behind it
    has a seasonal fact about every month of the year — put them all on the
    page and the page becomes an almanac, with the two findings that could
    change a decision buried in ten that could not. The profile chart carries
    the whole year; the cards carry what is coming.

    Three guards keep a coincidence from being called a season. The category
    must be big enough to matter at all, or a hobby averaging two euros
    produces an index of twenty-six. The deviation clears a higher floor than
    a one-off finding does, because this one claims something about every
    year. And the observed years must actually agree: a median puts half of
    them on the high side by construction, so without a consistency test two
    memorable Junes and three ordinary ones read exactly like a season.

    Size is measured as a monthly average rather than as a typical month,
    because the most seasonal categories there are do not have one. Gifts
    happen in December and at nobody's birthday in between, so its median
    month is zero — and a guard written against the median throws away the
    clearest season in the file while keeping the noise it was aimed at.
    """
    if len(months) < MIN_SEASON_MONTHS:
        return []

    rows = _spend_rows(transactions)
    by_major: dict[str, list] = defaultdict(list)
    for row in rows:
        by_major[row.major_category or "Other"].append(row)

    # The calendar months a card can still be acted on for, and how far off
    # each of them is.
    last = int(months[-1][5:])
    horizon = {
        (last + step - 1) % 12 + 1: step for step in range(1, SEASON_HORIZON_MONTHS + 1)
    }

    findings = []
    for major, major_rows in by_major.items():
        spend = _sum_by_month(major_rows)
        values = [spend.get(month, 0.0) for month in months]
        overall = _median(values)
        if sum(values) / len(months) < floor:
            continue

        for number, away in horizon.items():
            same = [month for month in months if int(month[5:]) == number]
            if len(same) < MIN_SEASON_YEARS:
                continue

            observed = [spend.get(month, 0.0) for month in same]
            typical = _median(observed)

            # A category that only exists in its season has no ordinary month
            # to be a multiple of. That is not a missing answer, it is the
            # strongest possible version of the one being looked for, so the
            # index is simply withheld and the euro test decides.
            index = typical / overall if overall else None
            if index is not None and SEASON_LOW < index < SEASON_HIGH:
                continue
            if abs(typical - overall) < SEASON_FLOOR_MULTIPLE * floor:
                continue

            agreeing = sum(
                1 for value in observed if (value > overall) == (typical > overall)
            )
            if agreeing < SEASON_CONSISTENCY * len(observed):
                continue

            findings.append(
                Pattern(
                    kind=KIND_SEASONAL,
                    subject=f"{major}|{number:02d}",
                    month="",
                    label=major,
                    category=major,
                    headline=round(typical - overall, 2),
                    facts={
                        "calendar_month": number,
                        "typical": round(typical, 2),
                        "usual": round(overall, 2),
                        "index": round(index, 2) if index else None,
                        "years": len(same),
                        "agreeing": agreeing,
                        "months_away": away,
                    },
                    series=tuple(
                        (month, round(spend.get(month, 0.0), 2)) for month in same
                    ),
                    transaction_ids=_proof(
                        [r for r in major_rows if month_key(r.date) in set(same)]
                    ),
                )
            )
    return findings


# --- Merchants coming and going ----------------------------------------------


def _churn(
    transactions: Sequence,
    months: Sequence[str],
    floor: float,
    scheduled_keys: set[str],
    reference: datetime,
) -> list[Pattern]:
    """
    Regulars that have gone quiet, and newcomers that have become regulars.

    Neither is visible in a category chart, because both usually happen inside
    a category that barely moves: the gym you stopped going to and the gym you
    started are both Fitness & Sports.

    Silence is measured against the merchant's own rhythm rather than a fixed
    number of weeks. A supermarket visited twice a week is gone after a month;
    a barber is not gone after two. Three of its own typical gaps, and never
    less than :data:`GONE_QUIET_MIN_DAYS`, is what separates "stopped" from
    "between visits".

    Merchants on a detected schedule are left out entirely — the whole
    merchant, not merely the charges that belong to the run. That they charge
    on a rhythm and have stopped is the recurring page's subject, and saying
    it here as well doubles every cancelled subscription. Excluding only the
    scheduled charges is not enough: a university that bills a monthly fee
    also sells the odd coffee, and those leftovers went quiet on exactly the
    same day, which reported the same cancellation twice with two different
    amounts.
    """
    rows = _spend_rows(transactions)
    keys = _merchant_keys(rows)

    by_merchant: dict[str, list] = defaultdict(list)
    for row in rows:
        resolved = keys.get(str(row.id))
        if resolved and resolved[0] not in scheduled_keys:
            by_merchant[resolved[0]].append(row)

    window = set(months[-ARRIVED_WINDOW_MONTHS:])
    findings = []
    for key, merchant_rows in by_merchant.items():
        merchant_rows.sort(key=lambda row: row.date)
        display = keys[str(merchant_rows[0].id)][1]
        first, last = merchant_rows[0].date, merchant_rows[-1].date
        span_days = (last - first).days
        total = sum(_spend_of(row) for row in merchant_rows)

        # Spending per month while the merchant was actually part of life,
        # which is what stopping frees up or starting costs.
        active_months = max(1, _index(month_key(last)) - _index(month_key(first)) + 1)
        monthly = total / active_months

        if (
            len(merchant_rows) >= MIN_REGULAR_VISITS
            and span_days >= MIN_REGULAR_SPAN_DAYS
            and monthly >= floor
        ):
            gaps = [
                (b.date - a.date).days for a, b in zip(merchant_rows, merchant_rows[1:])
            ]
            typical_gap = _median(gaps)
            silence = (reference - last).days
            if silence >= max(GONE_QUIET_MIN_DAYS, GONE_QUIET_GAPS * typical_gap):
                findings.append(
                    Pattern(
                        kind=KIND_GONE,
                        subject=key,
                        month=month_key(last),
                        label=display,
                        category=merchant_rows[-1].minor_category or "Uncategorized",
                        headline=round(-monthly, 2),
                        facts={
                            "visits": len(merchant_rows),
                            "last_seen": last.strftime("%Y-%m-%d"),
                            "silent_days": silence,
                            "typical_gap": round(typical_gap, 1),
                            "monthly": round(monthly, 2),
                        },
                        series=_series_of(
                            _sum_by_month(merchant_rows), months[-SPARK_MONTHS:]
                        ),
                        transaction_ids=_proof(merchant_rows),
                    )
                )
            continue

        if (
            month_key(first) in window
            and len(merchant_rows) >= ARRIVED_MIN_VISITS
            and monthly >= floor
        ):
            findings.append(
                Pattern(
                    kind=KIND_ARRIVED,
                    subject=key,
                    month=month_key(first),
                    label=display,
                    category=merchant_rows[-1].minor_category or "Uncategorized",
                    headline=round(monthly, 2),
                    facts={
                        "visits": len(merchant_rows),
                        "first_seen": first.strftime("%Y-%m-%d"),
                        "total": round(total, 2),
                        "monthly": round(monthly, 2),
                    },
                    series=_series_of(
                        _sum_by_month(merchant_rows), months[-SPARK_MONTHS:]
                    ),
                    transaction_ids=_proof(merchant_rows),
                )
            )
    return findings


# --- Composition, income and cash --------------------------------------------


def _mix(
    transactions: Sequence,
    months: Sequence[str],
    spend: dict[str, float],
    floor: float,
) -> list[Pattern]:
    """
    The total held and the composition moved.

    A substitution is invisible in every total on the dashboard: eating in
    less and eating out more is one bar going up, one going down, and a
    monthly figure that never moves. It is worth one card, and only when the
    total really did hold — otherwise the shares moved because the total did,
    which is a different and already-reported fact.

    Stated as a co-movement and nothing more. Two categories trading five
    points of share is not evidence that one caused the other, and the card
    says what happened rather than why.
    """
    recent, baseline = _windows(months)
    if len(baseline) < MIN_BASELINE_MONTHS:
        return []

    before, after = _level(spend, baseline), _level(spend, recent)
    if not before or abs(after - before) > MIX_STABLE_SHARE * before:
        return []

    by_major: dict[str, list] = defaultdict(list)
    for row in _spend_rows(transactions):
        by_major[row.major_category or "Other"].append(row)

    shares = {}
    for major, rows in by_major.items():
        series = _sum_by_month(rows)
        share_by_month = {
            month: series.get(month, 0.0) / spend[month]
            for month in months
            if spend.get(month)
        }
        shares[major] = _level(share_by_month, recent) - _level(
            share_by_month, baseline
        )

    if not shares:
        return []
    up = max(shares.items(), key=lambda item: item[1])
    down = min(shares.items(), key=lambda item: item[1])
    if up[1] < MIX_MIN_POINTS or down[1] > -MIX_MIN_POINTS:
        return []

    moved = min(up[1], -down[1]) * after
    if moved < floor:
        return []

    window = set(recent)
    return [
        Pattern(
            kind=KIND_MIX,
            subject=f"{up[0]}|{down[0]}",
            month=recent[-1],
            label=up[0],
            category=up[0],
            headline=round(moved, 2),
            facts={
                "up": up[0],
                "up_points": round(up[1] * 100, 1),
                "down": down[0],
                "down_points": round(down[1] * 100, 1),
                "total": round(after, 2),
                "total_before": round(before, 2),
            },
            series=_series_of(spend, months[-SPARK_MONTHS:]),
            transaction_ids=_proof(
                [r for r in by_major[up[0]] if month_key(r.date) in window]
            ),
        )
    ]


def _savings(
    transactions: Sequence,
    months: Sequence[str],
    spend: dict[str, float],
    income: dict[str, float],
    floor: float,
) -> list[Pattern]:
    """
    The share of what came in that did not go out again.

    The one measure on the page that cannot be read off a spending chart at
    all: spending can fall while the share kept falls too, if income fell
    faster. Months with no income are left out rather than counted as zero —
    on a ledger where the salary occasionally lands a day early, a month
    without one is a calendar artefact, not a month of infinite spending.
    """
    recent, baseline = _windows(months)
    if len(baseline) < MIN_BASELINE_MONTHS:
        return []

    rate = {
        month: (income[month] - spend.get(month, 0.0)) / income[month]
        for month in months
        if income.get(month, 0.0) > 0
    }
    if len([m for m in baseline if m in rate]) < MIN_BASELINE_MONTHS:
        return []

    before = _median([rate[m] for m in baseline if m in rate])
    after = _median([rate[m] for m in recent if m in rate] or [0.0])
    if abs(after - before) < SAVINGS_MIN_MOVE:
        return []

    earned = _level(income, recent)
    window = set(recent)
    return [
        Pattern(
            kind=KIND_SAVINGS,
            subject="",
            month=recent[-1],
            label="Share of income kept",
            headline=round((after - before) * earned, 2),
            facts={
                "before": round(before, 4),
                "after": round(after, 4),
                "income": round(earned, 2),
                "spend": round(_level(spend, recent), 2),
            },
            series=tuple(
                (month, round(rate.get(month, 0.0), 4))
                for month in months[-SPARK_MONTHS:]
            ),
            transaction_ids=_proof(
                [r for r in _spend_rows(transactions) if month_key(r.date) in window]
            ),
        )
    ]


def _cash(
    transactions: Sequence,
    months: Sequence[str],
    floor: float,
    big: float,
) -> list[Pattern]:
    """
    How much is being taken out in cash, and whether that has changed.

    A withdrawal is a transfer and stays out of every expense total, which is
    right — the money has not been spent yet and what it turns into never
    reaches the bank. But *how much of life runs on cash* is a real pattern,
    and a ledger where it moves is a ledger where the categorised spending is
    covering less than it used to.
    """
    recent, baseline = _windows(months)
    if len(baseline) < MIN_BASELINE_MONTHS:
        return []

    rows = [
        t
        for t in transactions
        if getattr(t, "minor_category", None) == MINOR_CASH_WITHDRAWAL
    ]
    if not rows:
        return []

    series = _sum_by_month(rows)
    before, after = _level(series, baseline), _level(series, recent)
    delta = after - before
    if not _material(delta, before, floor, big):
        return []

    window = set(recent)
    return [
        Pattern(
            kind=KIND_CASH,
            subject=MINOR_CASH_WITHDRAWAL,
            month=recent[-1],
            label=MINOR_CASH_WITHDRAWAL,
            category=MINOR_CASH_WITHDRAWAL,
            headline=round(delta, 2),
            facts={
                "before": round(before, 2),
                "after": round(after, 2),
                **_decompose(rows, recent, baseline),
            },
            series=_series_of(series, months[-SPARK_MONTHS:]),
            transaction_ids=_proof([r for r in rows if month_key(r.date) in window]),
        )
    ]


# --- The whole analysis ------------------------------------------------------


def _fixed_series(
    transactions: Sequence,
    months: Sequence[str],
    scheduled: set[str],
) -> dict[str, float]:
    """
    How much of each month's spending was already decided before it started.

    Taken from the charges themselves rather than from the commitment floor,
    because the two answer different questions: the floor is what the
    schedules imply about a normal month, and this is what actually left the
    account. A yearly premium sits in one month here and in twelve there, and
    both are true.
    """
    rows = [r for r in _spend_rows(transactions) if str(r.id) in scheduled]
    series = _sum_by_month(rows)
    return {month: series.get(month, 0.0) for month in months}


def _deduplicate(findings: Sequence[Pattern]) -> list[Pattern]:
    """
    Drop the findings that are another finding wearing a wider name.

    A schedule that stopped collecting is also a hole in whatever category it
    was filed under, so a cancelled rent arrives twice: once as "this standing
    order has stopped", naming the merchant and the month, and once as
    "Housing is down 447 a month". Both are true and only the first is worth
    reading — it is the same euros, said with the answer in it.

    Only the wider finding goes, and only when the narrower one accounts for
    most of it. A category that moved for several reasons at once, one of them
    a cancelled schedule, is still a category that moved.
    """
    explained = set()
    for pattern in findings:
        if pattern.kind not in (KIND_LAPSED, KIND_PRICE):
            continue
        minor = pattern.category
        names = {minor, MAJOR_BY_MINOR.get(minor, "")}
        for other in findings:
            if other.kind != KIND_DRIFT or other.subject not in names:
                continue
            if other.headline * pattern.headline <= 0:
                continue
            if abs(pattern.headline) >= DRIFT_EXPLAINS_SHARE * abs(other.headline):
                explained.add(other.key)
    return [pattern for pattern in findings if pattern.key not in explained]


def analyse(
    transactions: Sequence,
    schedules: Sequence[Schedule] = (),
    trip_of: dict[str, str] | None = None,
    reference: datetime | None = None,
) -> Analysis:
    """
    Every pattern in one ledger, ranked by what it is worth.

    Ranked by euros a month and nothing else. A finding's interest is not its
    novelty or its recency: a subscription that quietly went up four euros
    outranks a category that doubled from nothing, because one of them is
    money and the other is a percentage. Chronology in particular is the wrong
    order — the newest finding is not the largest, and a page sorted by date
    buries the rent under a coffee shop.

    Args:
        transactions: The whole history. Filtered here rather than by the
            caller, so every series is built from the same set.
        schedules: What the recurring page detected, with the user's overrides
            already applied. Empty is fine; the commitment findings simply do
            not appear.
        trip_of: Transaction id to trip name, for attributing expensive
            months. A month containing a holiday is not an anomaly.
        reference: How current the data is. Defaults to the last transaction
            in the history, never to today: statements arrive in batches, so
            today runs ahead of everything the database knows and every
            merchant looks abandoned between two uploads.

    Returns:
        The structural series the page draws, and the ranked findings.
    """
    transactions = list(transactions)
    months = complete_months(transactions)
    if not months:
        return Analysis((), (), (), (), (), (), (), 0.0, "")

    trip_of = trip_of or {}
    if reference is None:
        reference = max((t.date for t in transactions), default=datetime.now())

    # Only the complete months feed the statistics; the partial tail would
    # otherwise be the lowest month in every series it appears in.
    kept = set(months)
    rows = [t for t in transactions if month_key(t.date) in kept]

    spend = _sum_by_month(_spend_rows(rows))
    income = _sum_by_month(_income_rows(rows), value=lambda t: t.amount)
    scheduled = {tid for schedule in schedules for tid in schedule.transaction_ids}
    scheduled_keys = {schedule.merchant_key for schedule in schedules}
    committed = _commitment_series(schedules, months)
    fixed = _fixed_series(rows, months, scheduled)

    # Everything absolute on this page is a share of this number, so the same
    # thresholds mean the same thing on any ledger.
    normal = _median([spend.get(month, 0.0) for month in months])
    floor = FLOOR_SHARE * normal
    big = DRIFT_BIG_MOVE_SHARE * normal

    findings: list[Pattern] = []
    findings += _drift(rows, months, floor, big)
    findings += _outliers(rows, months, spend, trip_of, schedules)
    findings += _from_schedules(schedules, months, committed, floor)
    findings += _seasons(rows, months, floor)
    findings += _churn(rows, months, floor, scheduled_keys, reference)
    findings += _mix(rows, months, spend, floor)
    findings += _savings(rows, months, spend, income, floor)
    findings += _cash(rows, months, floor, big)

    findings = _deduplicate(findings)
    findings.sort(key=lambda pattern: (-abs(pattern.headline), pattern.kind))

    return Analysis(
        months=tuple(months),
        spend=tuple(round(spend.get(month, 0.0), 2) for month in months),
        income=tuple(round(income.get(month, 0.0), 2) for month in months),
        committed=tuple(round(committed.get(month, 0.0), 2) for month in months),
        fixed=tuple(round(fixed.get(month, 0.0), 2) for month in months),
        seasonal=tuple(_seasonal_profile(months, spend)),
        findings=tuple(findings),
        floor=round(floor, 2),
        reference=months[-1],
    )
