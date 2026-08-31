"""
Where the money goes, below the category level.

The dashboard answers "how much on groceries" and stops exactly where the
question gets interesting. Two ledgers spending the same four hundred euros a
month on food are not the same ledger: one buys from a single shop and the
other from twenty, and only one of those is a habit that a decision could
change. This module is the part that looks underneath a category total and
says who was actually paid.

Three questions of one window, and none of them is a change over time —
:mod:`app.patterns` owns that. These are levels, true whether or not anything
moved.

**Who gets the money.** Merchants ranked by what they were paid, each with the
share of all spending it holds, what a visit costs and how often one happens.

**How few of them there are.** The cumulative curve, and the two numbers that
summarise it: how many merchants make up half the spending, and how many make
up four fifths. Half the money going to nine merchants is a ledger somebody
could change by deciding one thing; the same half spread over ninety is not,
and no chart the dashboard already draws can tell those two apart.

**Who owns a category.** Inside Groceries or Public Transit, the share the
largest merchant holds. This is where a category total acquires a name.

Three rules hold, and each is borrowed from the module next to this one.

**Complete months only.** The trailing month is a few days of a statement that
has not landed, so :func:`app.patterns.complete_months` drops it. Ranking a
merchant on a four-day month reports whoever happened to be paid on the first.

**One merchant is what every other page calls one merchant.** Resolution goes
through :func:`app.normalization.merchant_of` — so the payee recovered from a
PayPal export wins over Comdirect's own line, which says only "PayPal" — and
then through the canonical map, so a name that a mid-word line wrap split into
two spellings is one merchant here as well.

**Shares are of everything spent, not of what could be named.** A booking line
holding no merchant at all still spent money, so it is reported as its own
figure rather than divided silently among the merchants that could be named.
A ranking whose shares add up to more than the ledger is a ranking that lies
about how concentrated it is.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass
from statistics import median
from typing import Sequence

from app.categories import flow_type_for, is_valid_major
from app.normalization import (
    build_canonical_map,
    merchant_key,
    merchant_of,
    normalize_merchant,
)
from app.patterns import complete_months, month_key
from app.utils import should_exclude_transaction

#: How many months the ranking is taken over. A year, because anything
#: shorter ranks the merchants of a season — a ledger read in January would
#: put the Christmas shops at the top and call it a habit.
WINDOW_MONTHS = 12

#: How many ranked merchants the page draws. Past this the rows are worth
#: less than the space, and the curve above them already says how long the
#: tail is.
TOP_MERCHANTS = 40

#: The rank the headline share is taken at. Ten is small enough that a person
#: could name them.
LEADERBOARD = 10

#: How many merchants a category needs before its leader's share says
#: anything. A category with one merchant is trivially 100% concentrated,
#: which is a fact about the taxonomy rather than about the spending.
MIN_CATEGORY_MERCHANTS = 3

#: How many categories the concentration list holds. Ranked by euros, so this
#: keeps the list to the ones where a leader's share is worth acting on.
TOP_CATEGORIES = 12

#: Transactions carried with each merchant, largest first, so the claim can be
#: opened and checked in one click.
PROOF_ROWS = 8


@dataclass(frozen=True)
class Merchant:
    """One merchant's window, and the window before it for comparison."""

    key: str
    name: str
    category: str
    major: str
    total: float
    share: float
    visits: int
    ticket: float
    first_seen: str
    last_seen: str
    previous_total: float | None
    previous_visits: int | None
    previous_ticket: float | None
    series: tuple[tuple[str, float], ...]
    transaction_ids: tuple[str, ...]


@dataclass(frozen=True)
class CategoryShare:
    """How much of one category's spending its largest merchants hold."""

    category: str
    total: float
    share: float
    merchants: int
    leader: str
    leader_share: float
    top_share: float


@dataclass(frozen=True)
class Concentration:
    """Everything the merchants page draws."""

    months: tuple[str, ...]
    previous: tuple[str, ...]
    history: tuple[str, ...]
    total: float
    unnamed: float
    count: int
    leaderboard_share: float
    half: int | None
    four_fifths: int | None
    curve: tuple[float, ...]
    merchants: tuple[Merchant, ...]
    categories: tuple[CategoryShare, ...]


def _flow(transaction) -> str:
    """
    Income, expense or transfer, derived from the major category as everywhere.

    A transaction from before the taxonomy migration has no usable major and
    falls back to the check the charts use.
    """
    major = getattr(transaction, "major_category", None)
    if major and is_valid_major(major):
        return flow_type_for(major)
    if should_exclude_transaction(transaction):
        return "transfer"
    return "income" if transaction.amount > 0 else "expense"


def _spend(transaction) -> float:
    """Spending as a positive number, so a refund nets off what it refunded."""
    return -transaction.amount


def _resolve(transactions: Sequence) -> dict[str, tuple[str, str]]:
    """
    Every transaction's merchant, as a key and the name to show it under.

    The recovered PayPal payee goes through the same normalizer as a booking
    line rather than being taken verbatim: the two spellings of one merchant
    would otherwise differ in case alone, and a shop paid sometimes by card
    and sometimes through PayPal would be ranked as two.
    """
    names = {str(t.id): normalize_merchant(merchant_of(t)) for t in transactions}
    canonical = build_canonical_map(name for name in names.values() if name)
    resolved = {}
    for tid, name in names.items():
        if not name:
            continue
        display = canonical.get(name, name)
        resolved[tid] = (merchant_key(display), display)
    return resolved


def _rank_at(curve: Sequence[float], threshold: float) -> int | None:
    """
    How many merchants it takes to reach a share of everything spent.

    ``None`` where the curve never gets there, which happens when enough of
    the spending carries no merchant name. Reporting a rank anyway would mean
    quoting a number the ledger does not contain.
    """
    for index, cumulative in enumerate(curve, start=1):
        if cumulative >= threshold:
            return index
    return None


def _dominant(rows: Sequence) -> tuple[str, str]:
    """The major and minor a merchant is mostly filed under, by euros."""
    weight: Counter = Counter()
    for row in rows:
        pair = (
            row.major_category or "Uncategorized",
            row.minor_category or "Needs Review",
        )
        weight[pair] += max(_spend(row), 0.0)
    if not weight:
        return "Uncategorized", "Needs Review"
    major, minor = weight.most_common(1)[0][0]
    return major, minor


def _ticket(rows: Sequence) -> float:
    """The median of what one visit costs, refunds left out of the median."""
    charges = [_spend(row) for row in rows if _spend(row) > 0]
    return round(median(charges), 2) if charges else 0.0


def _categories(
    rows: Sequence,
    resolved: dict[str, tuple[str, str]],
    total: float,
) -> tuple[CategoryShare, ...]:
    """
    The categories where one merchant holds most of the spending.

    Reported about the minor category, because that is the level a leader's
    share is actionable at: "most of Groceries is one shop" is a fact somebody
    can do something with, and "most of Food & Drink is groceries" is not.
    """
    by_category: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in rows:
        key = resolved.get(str(row.id), ("", ""))[0]
        if not key:
            continue
        by_category[row.minor_category or "Needs Review"][key] += _spend(row)

    names = {key: name for key, name in resolved.values()}
    shares = []
    for category, merchants in by_category.items():
        held = {key: value for key, value in merchants.items() if value > 0}
        if len(held) < MIN_CATEGORY_MERCHANTS:
            continue
        spent = sum(held.values())
        if spent <= 0:
            continue
        ordered = sorted(held.items(), key=lambda item: -item[1])
        shares.append(
            CategoryShare(
                category=category,
                total=round(spent, 2),
                share=round(spent / total, 4) if total else 0.0,
                merchants=len(held),
                leader=names.get(ordered[0][0], ordered[0][0]),
                leader_share=round(ordered[0][1] / spent, 4),
                top_share=round(
                    sum(value for _, value in ordered[:3]) / spent,
                    4,
                ),
            )
        )
    shares.sort(key=lambda share: -share.total)
    return tuple(shares[:TOP_CATEGORIES])


def _empty(history: Sequence[str]) -> Concentration:
    return Concentration(
        months=(),
        previous=(),
        history=tuple(history),
        total=0.0,
        unnamed=0.0,
        count=0,
        leaderboard_share=0.0,
        half=None,
        four_fifths=None,
        curve=(),
        merchants=(),
        categories=(),
    )


def analyse(
    transactions: Sequence,
    window: int = WINDOW_MONTHS,
    top: int = TOP_MERCHANTS,
) -> Concentration:
    """
    Rank the merchants of the last complete year and say how few they are.

    A merchant whose window nets out at nothing or less is left out: the
    ranking answers where the money went, and a shop that refunded more than
    it took has no place in it — the transactions are still on the category's
    total, where the arithmetic belongs.

    Args:
        transactions: Everything in the ledger. Filtered here, so callers do
            not have to agree on what counts as spending.
        window: How many complete months to rank over.
        top: How many ranked merchants to return in full. The curve and the
            counts are computed over all of them regardless.
    """
    history = tuple(complete_months(transactions))
    rows = [t for t in transactions if _flow(t) == "expense"]
    if not history or not rows:
        return _empty(history)

    recent = history[-window:]
    previous = history[-2 * window : -window] if len(history) >= 2 * window else ()
    recent_set, previous_set = set(recent), set(previous)

    resolved = _resolve(rows)

    lifetime: dict[str, list] = defaultdict(list)
    unnamed = 0.0
    in_window = []
    for row in rows:
        current = month_key(row.date) in recent_set
        if current:
            in_window.append(row)
        key = resolved.get(str(row.id), ("", ""))[0]
        if not key:
            if current:
                unnamed += _spend(row)
            continue
        lifetime[key].append(row)

    total = sum(_spend(row) for row in in_window)
    if total <= 0:
        return _empty(history)

    windows = {
        key: [row for row in items if month_key(row.date) in recent_set]
        for key, items in lifetime.items()
    }
    totals = {
        key: round(sum(_spend(row) for row in items), 2)
        for key, items in windows.items()
        if items and sum(_spend(row) for row in items) > 0
    }
    order = sorted(totals, key=lambda key: -totals[key])

    running = 0.0
    curve = []
    for key in order:
        running += totals[key]
        curve.append(round(running / total, 4))

    merchants = []
    for key in order[:top]:
        items = windows[key]
        prior = [row for row in lifetime[key] if month_key(row.date) in previous_set]
        series: dict[str, float] = defaultdict(float)
        for row in lifetime[key]:
            series[month_key(row.date)] += _spend(row)
        dates = sorted(row.date for row in lifetime[key])
        major, minor = _dominant(items)
        merchants.append(
            Merchant(
                key=key,
                name=resolved[str(items[0].id)][1],
                category=minor,
                major=major,
                total=totals[key],
                share=round(totals[key] / total, 4),
                visits=sum(1 for row in items if _spend(row) > 0),
                ticket=_ticket(items),
                first_seen=dates[0].strftime("%Y-%m-%d"),
                last_seen=dates[-1].strftime("%Y-%m-%d"),
                previous_total=(
                    round(sum(_spend(row) for row in prior), 2) if previous else None
                ),
                previous_visits=(
                    sum(1 for row in prior if _spend(row) > 0) if previous else None
                ),
                previous_ticket=_ticket(prior) if previous else None,
                series=tuple(
                    (month, round(series.get(month, 0.0), 2)) for month in history
                ),
                transaction_ids=tuple(
                    str(row.id)
                    for row in sorted(items, key=lambda r: (-abs(r.amount), r.date))[
                        :PROOF_ROWS
                    ]
                ),
            )
        )

    return Concentration(
        months=tuple(recent),
        previous=tuple(previous),
        history=history,
        total=round(total, 2),
        unnamed=round(unnamed, 2),
        count=len(order),
        leaderboard_share=round(
            sum(totals[key] for key in order[:LEADERBOARD]) / total, 4
        ),
        half=_rank_at(curve, 0.5),
        four_fifths=_rank_at(curve, 0.8),
        curve=tuple(curve),
        merchants=tuple(merchants),
        categories=_categories(in_window, resolved, total),
    )
