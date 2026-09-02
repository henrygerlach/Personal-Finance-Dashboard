"""
Payments that repeat on a schedule, and what they cost now.
"""


import calendar
from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from app.config import get_logger
from app.models import (
    OVERRIDE_ACTIVE,
    OVERRIDE_DECISIONS,
    OVERRIDE_DISMISSED,
    OVERRIDE_ENDED,
    RecurringOverride,
    Transaction,
)
from app.normalization import merchant_key
from app.recurrence import (
    AMOUNT_TOLERANCE,
    PERIOD_MONTHS,
    find_recurring,
    recurring_transaction_ids,
)
from app.services.common import _describe_transaction
from app.utils import (
    should_exclude_transaction,
)

logger = get_logger(__name__)

logger = get_logger(__name__)


#: How long a schedule may be late before it counts as ended rather than
#: merely due. Expressed as a share of its own interval, because lateness
#: scales: a monthly charge a month late has stopped, a yearly one has not.
OVERDUE_GRACE_SHARE = 0.25

#: ...but never less than this, so a direct debit that slid over a long
#: weekend is not reported as cancelled.
MIN_OVERDUE_GRACE_DAYS = 8

#: How far ahead the "what is coming" strip looks.
UPCOMING_DAYS = 30

#: How far a schedule's amount must move before it counts as a price change
#: rather than the ordinary wobble of a variable bill.
PRICE_CHANGE_THRESHOLD = 0.05

#: How close two charges must sit to count as the same price level. Tighter
#: than the detector's own tolerance, which is deliberately loose enough to
#: keep a price rise inside one schedule rather than splitting it in two.
PRICE_LEVEL_TOLERANCE = 0.02

#: How many charges must sit at a level before it counts as a level rather
#: than one odd month. Applies to both the current price and the old one.
MIN_CHARGES_PER_PRICE_LEVEL = 2


def _add_months(moment: datetime, months: int) -> datetime:
    """
    The same day of the month, ``months`` later.

    Clamped to the length of the target month, so a charge on the 31st is
    projected onto the 30th of a short month rather than overflowing into the
    next one — which is what the bank does with it too.
    """
    total = moment.month - 1 + months
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _current_price(charges: list) -> tuple[float, Optional[dict]]:
    """
    What a schedule costs now, and the change that got it there.

    Both answers come from the same walk. Charges are read backwards while
    they stay at the same level, which finds where the current price began;
    everything before it is the old price.

    Two things fall out of that, and the second is why it is not simply a
    median. Detection tolerates fifteen percent of drift inside one run —
    which is what keeps a price rise from splitting a subscription into two
    schedules — so a rise hides inside the lifetime median and is never shown.
    Worse, that median is then what the page adds up: rent held at 405 for
    three years and raised to 447 last winter would be carried in "committed
    every month" at 405, understating a bill that is being paid in full every
    month. Splitting the charges down the middle does not rescue either
    answer, because on a long history both halves still have a median of 405.

    Args:
        charges: The schedule's transactions in date order.

    Returns:
        The amount currently being charged, and the old and new amount with
        the month the change took effect — or None where the price has held.
    """
    amounts = [charge.amount for charge in charges]
    fallback = median(amounts)
    current = amounts[-1]
    if not current:
        return fallback, None

    start = len(amounts)
    while (
        start
        and abs(amounts[start - 1] - current) / abs(current) <= PRICE_LEVEL_TOLERANCE
    ):
        start -= 1

    earlier, level = amounts[:start], amounts[start:]
    # One charge at a new size is an anomaly, not a new price — and since the
    # level is read off the end, that is exactly where an anomaly would land.
    if len(level) < MIN_CHARGES_PER_PRICE_LEVEL:
        return fallback, None

    now = median(level)
    if len(earlier) < MIN_CHARGES_PER_PRICE_LEVEL:
        return now, None

    before = median(earlier)
    if not before or abs(now - before) / abs(before) < PRICE_CHANGE_THRESHOLD:
        return now, None

    return now, {
        "from": round(before, 2),
        "to": round(now, 2),
        "changed_on": charges[start].date.strftime("%Y-%m"),
    }


def _charges_of(run, by_id: dict) -> list:
    """The transactions behind one run, oldest first."""
    return sorted(
        (by_id[tid] for tid in run.transaction_ids if tid in by_id),
        key=lambda transaction: transaction.date,
    )


def _matching_override(overrides: list, merchant: str, period: str, amount: float):
    """
    The hand-made decision that speaks for one run, if there is one.

    Matched on the merchant, the period and the amount — the triple that makes
    a run distinct — with the amount compared inside the detector's own
    tolerance rather than exactly. Using the same tolerance is what keeps the
    two halves consistent: two schedules the detector separates by amount get
    their own overrides, and a price rise that stays inside one run because it
    is within tolerance keeps the override that run already had.
    """
    key = merchant_key(merchant)
    for override in overrides:
        if override.merchant_key != key or override.period != period:
            continue
        if not override.amount:
            continue
        if abs(amount - override.amount) / abs(override.amount) <= AMOUNT_TOLERANCE:
            return override
    return None


def _dismissed(run, overrides: list, charges: list) -> bool:
    """Whether this run has been dismissed by hand as not a schedule."""
    if not charges:
        return False
    amount, _ = _current_price(charges)
    override = _matching_override(overrides, run.merchant, run.period, amount)
    return bool(override) and override.decision == OVERRIDE_DISMISSED


class RecurringService:
    """Payments that repeat on a schedule, replacing the Subscriptions category."""

    @staticmethod
    async def detect_and_store() -> list:
        """
        Re-run detection over the whole history and mark the transactions.

        Recomputed rather than updated incrementally: a new charge can turn
        two earlier ones into a schedule, and can end one that has stopped.
        """
        transactions = await Transaction.find_all().to_list()
        runs = find_recurring(transactions)
        overrides = await RecurringOverride.find_all().to_list()

        # A dismissed run is not a schedule, so its transactions must not
        # carry the flag either: the dashboard draws a "repeats" badge from
        # it, and a badge contradicting the recurring page is worse than no
        # badge at all.
        by_id = {str(t.id): t for t in transactions}
        kept = [
            run
            for run in runs
            if not _dismissed(run, overrides, _charges_of(run, by_id))
        ]
        recurring = recurring_transaction_ids(kept)

        for transaction in transactions:
            should_be = str(transaction.id) in recurring
            if transaction.is_recurring != should_be:
                transaction.is_recurring = should_be
                await transaction.save()

        logger.info(
            "Recurring detection: %s schedules (%s dismissed by hand) "
            "covering %s transactions",
            len(kept),
            len(runs) - len(kept),
            len(recurring),
        )
        return kept

    @staticmethod
    async def overview() -> dict:
        """
        Every detected schedule, told apart by whether it is still running.

        The page this backs answers one question first — what am I committed
        to right now — so the split that matters is not income against
        spending but *live* against *finished*. A subscription cancelled two
        years ago is still a real pattern in the history, and it must not sit
        in the totals or in the same visual weight as the rent.

        Quarterly, half-yearly and yearly charges are carried at their monthly
        equivalent so the totals mean something: a 90 EUR annual insurance is
        7.50 a month next to a 4.99 subscription.

        Detection is a heuristic and gets both of its judgements wrong
        sometimes, so a hand-made decision overrules it: a run the user has
        dismissed is not a schedule, and one they have marked still running or
        finished takes that status whatever its dates say.
        """
        transactions = await Transaction.find_all().to_list()
        runs = find_recurring(transactions)
        overrides = await RecurringOverride.find_all().to_list()
        by_id = {str(t.id): t for t in transactions}

        # "Now" is the end of the imported history, not today. Statements
        # arrive in batches, so between two uploads today runs ahead of
        # everything the database knows and every schedule looks abandoned.
        # Capped at today so a single future-dated line cannot move it.
        today = datetime.now()
        reference = min(max((t.date for t in transactions), default=today), today)

        items = []
        for run in runs:
            charges = _charges_of(run, by_id)
            if not charges:
                continue

            latest = charges[-1]
            months = PERIOD_MONTHS[run.period]
            next_expected = _add_months(latest.date, months)
            days_away = (next_expected - reference).days

            # A schedule is only over once it has missed by more than its own
            # tolerance for being late; until then it is simply due.
            grace = max(
                MIN_OVERDUE_GRACE_DAYS,
                round(months * 30.44 * OVERDUE_GRACE_SHARE),
            )
            if days_away >= 0:
                status = "active"
            elif -days_away <= grace:
                status = "due"
            else:
                status = "ended"

            # What it costs now, not what it has averaged: the page is a
            # statement of standing commitments, and last winter's rent is
            # not one of them.
            amount, price_change = _current_price(charges)

            # Kept alongside the final answer so the popup can say both: a
            # decision is only worth reviewing next to what it overruled.
            detected_status = status

            override = _matching_override(overrides, run.merchant, run.period, amount)
            if override and override.decision != OVERRIDE_DISMISSED:
                status = (
                    OVERRIDE_ACTIVE
                    if override.decision == OVERRIDE_ACTIVE
                    else OVERRIDE_ENDED
                )
            if status == "active" and days_away < 0:
                # Only reachable by hand: the dates say the next charge is
                # overdue and the user says the schedule is running anyway.
                # Rolled forward to the next occurrence that has not already
                # passed, because "expected 85 days ago" is not a useful thing
                # to say about a payment the user has just confirmed is alive.
                while days_away < 0:
                    next_expected = _add_months(next_expected, months)
                    days_away = (next_expected - reference).days

            monthly_equivalent = amount / months

            # Transfers are neither earning nor spending, so they are their
            # own direction rather than a signed amount.
            if should_exclude_transaction(latest):
                direction = "transfer"
            else:
                direction = "in" if amount > 0 else "out"
            items.append(
                {
                    # Stable enough to key a card and its popup: a run is one
                    # merchant on one schedule, and its first charge does not
                    # move when a new one arrives.
                    "id": f"{run.transaction_ids[0]}-{run.period}",
                    "merchant": run.merchant,
                    "period": run.period,
                    "occurrences": run.occurrences,
                    "amount": round(amount, 2),
                    "monthly_equivalent": round(monthly_equivalent, 2),
                    "annual_equivalent": round(monthly_equivalent * 12, 2),
                    "major": latest.major_category,
                    "minor": latest.minor_category,
                    "flow_type": latest.flow_type,
                    "direction": direction,
                    "status": status,
                    "first_seen": charges[0].date.strftime("%Y-%m-%d"),
                    "last_seen": latest.date.strftime("%Y-%m-%d"),
                    "next_expected": next_expected.strftime("%Y-%m-%d"),
                    "days_away": days_away,
                    # The day of the month it usually lands on, which is what
                    # a monthly schedule is actually recognised by.
                    "typical_day": Counter(
                        charge.date.day for charge in charges
                    ).most_common(1)[0][0],
                    "price_change": price_change,
                    "override": override.decision if override else None,
                    "detected_status": detected_status,
                    "charges": [
                        _describe_transaction(charge) for charge in reversed(charges)
                    ],
                }
            )

        def by_size(item: dict) -> float:
            """Largest commitment first — the eye should land on the rent."""
            return -abs(item["monthly_equivalent"])

        # A dismissed schedule is not a schedule, so it leaves the page
        # entirely — out of both directions, the totals and the strip. It is
        # still listed, quietly, because a decision has to be reversible.
        dismissed = [item for item in items if item["override"] == OVERRIDE_DISMISSED]
        kept = [item for item in items if item["override"] != OVERRIDE_DISMISSED]

        live = [item for item in kept if item["status"] != "ended"]
        outgoing = sorted(
            [item for item in live if item["direction"] == "out"], key=by_size
        )
        incoming = sorted(
            [item for item in live if item["direction"] == "in"], key=by_size
        )
        transfers = sorted(
            [item for item in live if item["direction"] == "transfer"], key=by_size
        )
        ended = sorted(kept, key=lambda item: item["last_seen"], reverse=True)
        ended = [item for item in ended if item["status"] == "ended"]
        dismissed.sort(key=lambda item: item["last_seen"], reverse=True)

        # Only what is still running belongs in a total: a cancelled
        # subscription in "recurring out per month" is a lie about the future.
        monthly_expense = round(sum(item["monthly_equivalent"] for item in outgoing), 2)
        monthly_income = round(sum(item["monthly_equivalent"] for item in incoming), 2)
        monthly_transfer = round(
            sum(item["monthly_equivalent"] for item in transfers), 2
        )

        # What is already spoken for before any discretionary spending. Only
        # meaningful against recurring income, so it is withheld when there is
        # none rather than divided by zero or by a guess.
        committed_share = (
            round(abs(monthly_expense) / monthly_income, 4)
            if monthly_income > 0
            else None
        )

        # Transfers are in here even though they are out of the totals: the
        # totals are about earning and spending, this strip is about what
        # actually leaves the account and when, and a savings plan does.
        horizon = reference + timedelta(days=UPCOMING_DAYS)
        upcoming = sorted(
            (
                item
                for item in live
                if datetime.strptime(item["next_expected"], "%Y-%m-%d") <= horizon
            ),
            key=lambda item: item["days_away"],
        )

        return {
            "items": items,
            "dismissed": dismissed,
            "outgoing": outgoing,
            "incoming": incoming,
            "transfers": transfers,
            "ended": ended,
            "upcoming": upcoming,
            "monthly_expense": monthly_expense,
            "monthly_income": monthly_income,
            "monthly_transfer": monthly_transfer,
            "annual_expense": round(monthly_expense * 12, 2),
            "annual_income": round(monthly_income * 12, 2),
            "committed_share": committed_share,
            "reference_date": reference.strftime("%Y-%m-%d"),
            # Statements are uploaded in batches, so the history normally
            # stops short of today. Every "next in N days" on the page counts
            # from the end of it, and that only means something if the page
            # also says how far back that is.
            "days_behind": (today - reference).days,
            "upcoming_days": UPCOMING_DAYS,
            "transfer_count": len(transfers),
            "dismissed_count": len(dismissed),
            "override_count": sum(1 for item in items if item["override"]),
            "active_count": len(live),
            "ended_count": len(ended),
            "overdue_count": sum(1 for item in live if item["status"] == "due"),
            "price_change_count": sum(1 for item in live if item["price_change"]),
            "count": len(kept),
        }

    @staticmethod
    async def _locate(schedule_id: str) -> tuple:
        """
        The detected run a schedule id names, and the price it carries now.

        Detection is re-run rather than the id trusted, because the id is only
        a handle the page hands back: the merchant, period and amount an
        override is stored against have to come from the detector, not from
        the browser.
        """
        anchor_id, _, period = schedule_id.partition("-")
        transactions = await Transaction.find_all().to_list()
        by_id = {str(t.id): t for t in transactions}

        for run in find_recurring(transactions):
            if run.period != period or run.transaction_ids[0] != anchor_id:
                continue
            charges = _charges_of(run, by_id)
            if not charges:
                break
            return run, _current_price(charges)[0]
        return None, None

    @staticmethod
    async def set_override(schedule_id: str, decision: str) -> dict:
        """
        Record what the user says about one schedule.

        Args:
            schedule_id: The id the page carries for a detected run.
            decision: One of ``dismissed``, ``active`` or ``ended``.

        Returns:
            The merchant and decision that were stored.

        Raises:
            ValueError: The decision is not one of the three, or no detected
                schedule answers to that id any more.
        """
        if decision not in OVERRIDE_DECISIONS:
            raise ValueError(f"Unknown decision {decision!r}")

        run, amount = await RecurringService._locate(schedule_id)
        if run is None:
            raise ValueError(f"No detected schedule for {schedule_id!r}")

        key = merchant_key(run.merchant)
        existing = await RecurringOverride.find(
            RecurringOverride.merchant_key == key,
            RecurringOverride.period == run.period,
        ).to_list()

        override = _matching_override(existing, run.merchant, run.period, amount)
        if override is None:
            override = RecurringOverride(
                merchant_key=key,
                display_name=run.merchant,
                period=run.period,
                amount=round(amount, 2),
                decision=decision,
            )
            await override.insert()
        else:
            override.decision = decision
            # Follow the price: the schedule this decision was made about is
            # the same one after a rise, and the stored amount is what a
            # later match is measured against.
            override.amount = round(amount, 2)
            override.display_name = run.merchant
            override.decided_at = datetime.utcnow()
            await override.save()

        logger.info(
            "Recurring override: %s %s %.2f -> %s",
            run.merchant,
            run.period,
            amount,
            decision,
        )
        return {"merchant": run.merchant, "decision": decision}

    @staticmethod
    async def clear_override(schedule_id: str) -> bool:
        """
        Hand one schedule back to the detector.

        Returns:
            Whether there was a decision to undo.
        """
        run, amount = await RecurringService._locate(schedule_id)
        if run is None:
            raise ValueError(f"No detected schedule for {schedule_id!r}")

        existing = await RecurringOverride.find(
            RecurringOverride.merchant_key == merchant_key(run.merchant),
            RecurringOverride.period == run.period,
        ).to_list()

        override = _matching_override(existing, run.merchant, run.period, amount)
        if override is None:
            return False
        await override.delete()
        logger.info("Recurring override cleared: %s %s", run.merchant, run.period)
        return True
