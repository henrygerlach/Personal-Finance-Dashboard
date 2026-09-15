"""
Detect payments that repeat on a schedule.

This replaces the old "Subscriptions" category, which was a category only in
name: whether Spotify is a subscription is not a property of what Spotify
sells, it is a property of the payment history. Asking a classifier would mean
asking it to guess something the data already knows, so nothing here involves
the model.

A run is recurring when the same merchant charges a similar amount at a
regular interval, at least a minimum number of times. All three conditions
matter:

*Same merchant* alone is useless — a supermarket appears hundreds of times.

*Similar amount* is what separates a subscription from ordinary shopping.
Rent and streaming barely move; a weekly grocery shop moves constantly, so the
amounts are clustered first and each cluster is examined on its own. A shop
that also happens to bill a fixed monthly fee is then still found.

*Regular interval* is checked between consecutive charges rather than on
average, because an average hides everything: twelve charges in one week and
one a year later average out to monthly.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from app.normalization import build_canonical_map, merchant_key, normalize_merchant

#: Intervals a recurring payment is allowed to follow, in days. Monthly is a
#: range because month lengths differ and a charge on the 31st slides.
#:
#: Ordered shortest first, because a candidate stops at the first period that
#: fits and the shortest one that fits is the one being followed.
PERIODS: dict[str, tuple[int, int]] = {
    "monthly": (28, 31),
    "quarterly": (89, 92),
    # Insurance and membership fees are commonly billed twice a year. Without
    # this they fell between quarterly and yearly and were found as neither.
    "half-yearly": (181, 186),
    "yearly": (363, 367),
}

#: How many months each period spans. The single place that knows, so a caller
#: converting a charge to its monthly equivalent or projecting the next one
#: does not restate the arithmetic.
PERIOD_MONTHS: dict[str, int] = {
    "monthly": 1,
    "quarterly": 3,
    "half-yearly": 6,
    "yearly": 12,
}

#: How far a single interval may fall outside its period. Direct debits move
#: around weekends and bank holidays.
INTERVAL_TOLERANCE_DAYS = 5

#: How far an amount may sit from the run's median. Streaming prices rise,
#: utility bills vary; a doubling is a different payment.
AMOUNT_TOLERANCE = 0.15

#: Below this, a share is the wrong ruler.
#:
#: Price rises are not proportional to price: a subscription going up by a
#: euro is a rounding error on the rent and a fifth of a streaming plan. At
#: fifteen percent, 4.99 to 5.99 is a different payment, so a service that got
#: one euro dearer left its own run and — being two or three charges on each
#: side of the rise — was then found as no schedule at all.
SMALL_AMOUNT = 20.0

#: What that same drift is allowed to be below ``SMALL_AMOUNT``. Wide enough
#: for the price rises cheap services actually make, and still narrow enough
#: that 4.99 and 9.99 are two different things.
SMALL_AMOUNT_TOLERANCE = 0.30

#: Below this many charges there is no schedule, only a coincidence.
MIN_OCCURRENCES = 3

#: How many billing periods one run may miss in total.
#:
#: One, not one per gap: a subscription occasionally fails to collect, but a
#: pattern held together by repeated absences is not a schedule. Allowing a
#: skip at every gap let three drugstore visits spread over five months
#: qualify as monthly.
MAX_SKIPPED_PERIODS = 1

#: What share of a merchant's similarly-sized charges inside the run's own
#: time span must belong to the run.
#:
#: This is what separates a subscription from a shop visited often. Somebody
#: who buys groceries three hundred times will, by chance alone, produce three
#: visits about a month apart for a similar amount — the first version of this
#: reported EDEKA as three separate monthly subscriptions. A subscription
#: charges on its schedule and at no other time, so inside the run's span
#: almost every comparable charge should be part of it.
MIN_SPAN_COVERAGE = 0.75


@dataclass(frozen=True)
class RecurringRun:
    """One detected schedule."""

    merchant: str
    period: str
    transaction_ids: tuple[str, ...]
    median_amount: float

    @property
    def occurrences(self) -> int:
        return len(self.transaction_ids)


def amount_tolerance(reference: float, tolerance: float = AMOUNT_TOLERANCE) -> float:
    """
    The drift allowed around one amount, as a share of it.

    A share alone does not work at both ends of a ledger, so the share widens
    for small amounts. Everything that compares an amount to a schedule's own
    amount goes through here — clustering, and matching a stored decision —
    because a rise that keeps a subscription in one run has to keep its
    decision attached to it as well.
    """
    if abs(reference) < SMALL_AMOUNT:
        return max(tolerance, SMALL_AMOUNT_TOLERANCE)
    return tolerance


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _cluster_by_amount(
    entries: Sequence[tuple[str, datetime, float]], tolerance: float
) -> list[list[tuple[str, datetime, float]]]:
    """
    Split a merchant's charges into groups of similar size.

    Without this, a shop that bills both a fixed monthly fee and ordinary
    purchases would have neither found: the fee's regular rhythm disappears
    into the noise of everything else.

    Sizes are compared with their sign, so a refund never joins the charge it
    reverses: 10 EUR paid and 10 EUR returned are 20 EUR apart, not identical,
    and reading them as one schedule reported a purchase and its refund as a
    standing commitment — at a median of zero, which then no stored decision
    could match either.
    """
    clusters: list[list[tuple[str, datetime, float]]] = []
    for entry in sorted(entries, key=lambda item: abs(item[2])):
        amount = entry[2]
        for cluster in clusters:
            reference = _median([item[2] for item in cluster])
            if reference == 0:
                continue
            if abs(amount - reference) / abs(reference) <= amount_tolerance(
                reference, tolerance
            ):
                cluster.append(entry)
                break
        else:
            clusters.append([entry])
    return clusters


def _periods_spanned(
    gap: int,
    period_name: str,
    period: tuple[int, int],
    tolerance_days: int,
) -> int:
    """
    How many billing periods one gap between charges covers.

    Returns 0 when the gap matches no whole number of periods, which ends the
    run.

    A monthly run may cover two periods, meaning one charge is missing.
    Without that, a single absent charge ends the run where it stands and
    everything after it is thrown away — so a subscription still being paid
    every month is reported as having stopped on whatever date its run
    happened to break, which is what a failed direct debit or a month billed
    at an odd amount actually looks like in the data.

    Only monthly runs may do this, for two reasons. A month is the one
    interval where a single miss is ordinary rather than a long silence. It is
    also the only one whose doubled gap cannot be read as a different period:
    two quarters is a half-year and two half-years is a year, so bridging
    those would report a half-yearly premium as quarterly and double what the
    page says it costs.
    """
    for multiple in (1, 2):
        if multiple == 2 and period_name != "monthly":
            return 0
        low = period[0] * multiple - tolerance_days
        high = period[1] * multiple + tolerance_days
        if low <= gap <= high:
            return multiple
    return 0


def _longest_run(
    entries: Sequence[tuple[str, datetime, float]],
    period_name: str,
    period: tuple[int, int],
    tolerance_days: int,
) -> tuple[list[tuple[str, datetime, float]], int]:
    """
    The longest stretch of consecutive charges spaced by one period.

    Checked pair by pair rather than as an average, because an average hides
    exactly the pattern this is meant to exclude: twelve charges in a week and
    one a year later average out to monthly.

    Returns:
        The run, and how many billing periods it had to skip to hold together.
        The caller needs the second number because a skip is an assumption
        rather than an observation, and a run should not qualify on one.
    """
    ordered = sorted(entries, key=lambda item: item[1])

    best: tuple[list, int] = ([], 0)
    current: list = ordered[:1]
    skipped = 0
    for previous, entry in zip(ordered, ordered[1:]):
        gap = (entry[1] - previous[1]).days
        spanned = _periods_spanned(gap, period_name, period, tolerance_days)
        if spanned and skipped + spanned - 1 <= MAX_SKIPPED_PERIODS:
            current.append(entry)
            skipped += spanned - 1
        else:
            best = max(best, (current, skipped), key=lambda pair: len(pair[0]))
            current, skipped = [entry], 0
    return max(best, (current, skipped), key=lambda pair: len(pair[0]))


def _span_coverage(
    run: Sequence[tuple[str, datetime, float]],
    merchant_entries: Sequence[tuple[str, datetime, float]],
    scheduled_elsewhere: set[str],
) -> float:
    """
    The share of the merchant's unscheduled charges in the span that the run holds.

    This is the one measure that separates a subscription from a shop visited
    often, and it has to look at everything the merchant charged in that
    window. Restricting the denominator by amount does not work: at a
    supermarket the amounts are spread continuously, so the only charges close
    enough to the run's median are the run itself and the ratio is always one.
    Somebody who shops three hundred times is then reported as running three
    monthly subscriptions.

    Charges that belong to *another* schedule of the same merchant are left
    out, because a charge that is itself on a schedule is not evidence against
    this one. Without that, an employer paying two standing orders would have
    each halve the other's coverage and neither would be found.

    A subscription bills on its schedule and at no other time, so its run is
    nearly all of what remains. A supermarket run has dozens of ordinary
    visits sitting between its members.
    """
    first, last = run[0][1], run[-1][1]
    own = {entry[0] for entry in run}
    inside = [
        entry
        for entry in merchant_entries
        if first <= entry[1] <= last
        and (entry[0] in own or entry[0] not in scheduled_elsewhere)
    ]
    return len(run) / len(inside) if inside else 1.0


def find_recurring(
    transactions: Iterable,
    min_occurrences: int = MIN_OCCURRENCES,
    amount_tolerance: float = AMOUNT_TOLERANCE,
    interval_tolerance_days: int = INTERVAL_TOLERANCE_DAYS,
    min_span_coverage: float = MIN_SPAN_COVERAGE,
) -> list[RecurringRun]:
    """
    Find every recurring schedule in a set of transactions.

    Args:
        transactions: Objects with ``id``, ``date``, ``amount``,
            ``booking_text`` and ``process``.

    Returns:
        One run per merchant, amount cluster and period that qualifies,
        longest first.
    """
    transactions = list(transactions)
    names = {
        str(t.id): normalize_merchant(t.booking_text, t.process) for t in transactions
    }
    canonical = build_canonical_map(name for name in names.values() if name)

    by_merchant: dict[str, list[tuple[str, datetime, float]]] = defaultdict(list)
    for transaction in transactions:
        name = names[str(transaction.id)]
        if not name:
            continue
        key = merchant_key(canonical.get(name, name))
        by_merchant[key].append(
            (str(transaction.id), transaction.date, transaction.amount)
        )

    runs: list[RecurringRun] = []
    for key, entries in by_merchant.items():
        if len(entries) < min_occurrences:
            continue
        display = next(canonical.get(names[tid], names[tid]) for tid, _, _ in entries)

        # Every regular stretch this merchant has, found first and judged
        # after: a candidate cannot be scored against the others until they
        # are all known.
        candidates: list[tuple[str, list]] = []
        for cluster in _cluster_by_amount(entries, amount_tolerance):
            if len(cluster) < min_occurrences:
                continue
            for period_name, period in PERIODS.items():
                run, skipped = _longest_run(
                    cluster, period_name, period, interval_tolerance_days
                )
                # A skipped period is an assumption that a charge should have
                # been there, so it must not be what gets a run over the line:
                # three drugstore visits with a month missing between two of
                # them are a coincidence, not a subscription.
                if len(run) >= min_occurrences + skipped:
                    candidates.append((period_name, run))
                    # A run that is monthly is not also quarterly; the first
                    # period that fits is the one it follows.
                    break

        for period_name, run in candidates:
            own = {entry[0] for entry in run}
            scheduled_elsewhere = {
                entry[0]
                for _, other in candidates
                for entry in other
                if entry[0] not in own
            }
            if _span_coverage(run, entries, scheduled_elsewhere) < min_span_coverage:
                # Charges happen at this merchant far more often than the run
                # claims; the rhythm is a coincidence.
                continue
            runs.append(
                RecurringRun(
                    merchant=display,
                    period=period_name,
                    transaction_ids=tuple(entry[0] for entry in run),
                    median_amount=_median([entry[2] for entry in run]),
                )
            )

    runs.sort(key=lambda run: (-run.occurrences, run.merchant))
    return runs


def recurring_transaction_ids(runs: Iterable[RecurringRun]) -> set[str]:
    """Every transaction that belongs to a detected schedule."""
    return {tid for run in runs for tid in run.transaction_ids}
