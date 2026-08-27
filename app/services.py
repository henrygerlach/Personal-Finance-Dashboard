"""
Business logic services for categorization and analytics.
"""

import asyncio
import calendar
from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from beanie import PydanticObjectId

from app.categories import MINOR_NEEDS_REVIEW, flow_type_for, is_valid_pair
from app.config import get_logger
from app.models import (
    OVERRIDE_ACTIVE,
    OVERRIDE_DECISIONS,
    OVERRIDE_DISMISSED,
    OVERRIDE_ENDED,
    SOURCE_USER,
    ComdirectBalanceSnapshot,
    RecurringOverride,
    Transaction,
)
from app.normalization import merchant_key, normalize_merchant
from app.recurrence import (
    AMOUNT_TOLERANCE,
    PERIOD_MONTHS,
    find_recurring,
    recurring_transaction_ids,
)
from app.utils import (
    generate_all_months_in_year,
    generate_month_range,
    group_by_month,
    group_by_month_and_category_field,
    should_exclude_transaction,
)

logger = get_logger(__name__)

#: How many transactions each direction of a drill-down returns. Enough to
#: show what actually drove the thing that was clicked, without turning the
#: popup into a statement listing.
TOP_DETAIL_TRANSACTIONS = 8


def _describe_transaction(transaction) -> dict:
    """
    One transaction as the dashboard's drill-down list needs it.

    The merchant is the recovered PayPal counterparty where there is one,
    because for those lines Comdirect's own booking text says only "PayPal".
    The raw booking text travels alongside it so the panel can show the line
    the bank actually recorded.
    """
    merchant = getattr(transaction, "paypal_counterparty", None) or normalize_merchant(
        transaction.booking_text, transaction.process
    )
    return {
        "id": str(transaction.id),
        "date": transaction.date.strftime("%Y-%m-%d"),
        "amount": transaction.amount,
        "merchant": merchant or "Unnamed payment",
        "booking_text": transaction.booking_text,
        "major": transaction.major_category or "Other",
        "minor": transaction.minor_category or "Uncategorized",
        "is_recurring": bool(getattr(transaction, "is_recurring", False)),
    }


def _summarize_side(transactions: list, limit: int) -> dict:
    """
    One direction of a bar: its top transactions and what they leave out.

    ``rest`` is what the rows beyond the limit add up to, so the panel can say
    how much of the bar it is not showing instead of implying the list is the
    whole story.

    Args:
        transactions: Every transaction on this side, already ranked.
        limit: How many of them to return.
    """
    shown = transactions[:limit]
    total = sum(transaction.amount for transaction in transactions)
    shown_total = sum(transaction.amount for transaction in shown)
    return {
        "transactions": [_describe_transaction(t) for t in shown],
        "count": len(transactions),
        "hidden_count": len(transactions) - len(shown),
        "total": total,
        "rest": total - shown_total,
    }


class ComdirectBalanceService:
    """Authenticate once and cache Comdirect balances in memory."""

    _snapshot: dict = {
        "status": "not-configured",
        "loaded_at": None,
        "error": None,
        "balances": None,
    }

    @classmethod
    async def initialize_startup_session(cls) -> None:
        """Authenticate once at startup and load account/card/depot balances."""
        from app.config import settings

        if not settings.comdirect_startup_auth_enabled:
            fallback_reason = "Comdirect startup auth is disabled by configuration"
            fallback_snapshot = await cls._load_latest_stored_snapshot(
                fallback_reason=fallback_reason
            )
            if fallback_snapshot:
                cls._snapshot = fallback_snapshot
                logger.info(
                    "Startup auth disabled; using latest stored Comdirect balance "
                    "snapshot"
                )
            else:
                cls._snapshot.update(
                    {
                        "status": "disabled-no-snapshot",
                        "loaded_at": None,
                        "error": (
                            "Startup auth disabled and no stored balance snapshot "
                            "is available"
                        ),
                        "balances": None,
                    }
                )
                logger.info(
                    "Comdirect startup auth disabled and no stored snapshot found"
                )
            return

        required = {
            "comdirect_client_id": settings.comdirect_client_id,
            "comdirect_client_secret": settings.comdirect_client_secret,
            "comdirect_access_number": settings.comdirect_access_number,
            "comdirect_pin": settings.comdirect_pin,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            message = f"Missing Comdirect credentials: {', '.join(missing)}"
            fallback_snapshot = await cls._load_latest_stored_snapshot(
                fallback_reason=message
            )
            if fallback_snapshot:
                cls._snapshot = fallback_snapshot
            else:
                cls._snapshot.update({"status": "error", "error": message})
            logger.warning(message)
            return

        logger.info("Starting Comdirect startup authentication")
        try:
            snapshot = await asyncio.to_thread(cls._authenticate_and_load)
            await cls._store_snapshot(snapshot)
            cls._snapshot = snapshot
            logger.info("Comdirect balances loaded at startup")
        except Exception as e:
            error_message = f"Failed to authenticate/load Comdirect balances: {e}"
            fallback_snapshot = await cls._load_latest_stored_snapshot(
                fallback_reason=error_message
            )
            if fallback_snapshot:
                cls._snapshot = fallback_snapshot
                logger.warning(
                    "Using stored Comdirect balance snapshot due to authentication "
                    "failure"
                )
            else:
                cls._snapshot.update({"status": "error", "error": error_message})
            logger.error(error_message)

    @classmethod
    def _authenticate_and_load(cls) -> dict:
        """Blocking Comdirect auth + data retrieval executed in thread."""
        from comdirect_api import ComdirectAuth, ComdirectClient

        from app.config import settings

        auth = ComdirectAuth(
            client_id=settings.comdirect_client_id,
            client_secret=settings.comdirect_client_secret,
            access_number=settings.comdirect_access_number,
            pin=settings.comdirect_pin,
        )
        auth.authenticate()
        client = ComdirectClient(auth)

        accounts = client.get_account_balances()
        cards = client.get_card_balances()
        depots = client.get_depot_ids()

        balances = {}
        for account in accounts:
            balances[account.account.accountType.text] = account.balance.value
        for card in cards:
            balances[card.balance.card.cardType.text] = card.balance.balance.value

        positions = []
        for depot in depots:
            positions.extend(client.get_depot_positions(depot.depotId))

        depot_balance = 0.0
        for position in positions:
            depot_balance += float(position.currentValue.value)
        balances["Depot"] = depot_balance

        return {
            "status": "ready",
            "loaded_at": datetime.utcnow().isoformat(),
            "error": None,
            "balances": balances,
        }

    @classmethod
    async def _store_snapshot(cls, snapshot: dict) -> None:
        """Persist a successful snapshot in MongoDB for fallback usage."""
        loaded_at_raw = snapshot.get("loaded_at")
        try:
            loaded_at = datetime.fromisoformat(loaded_at_raw)
        except Exception:
            loaded_at = datetime.utcnow()

        balances = snapshot.get("balances") or {}
        normalized_balances = {
            str(key): float(value or 0) for key, value in balances.items()
        }

        document = ComdirectBalanceSnapshot(
            loaded_at=loaded_at,
            balances=normalized_balances,
        )
        await document.insert()

    @classmethod
    async def _load_latest_stored_snapshot(cls, fallback_reason: str) -> dict | None:
        """Load latest persisted snapshot and mark it as stale fallback."""
        latest = await (
            ComdirectBalanceSnapshot.find_all().sort("-loaded_at").first_or_none()
        )
        if not latest:
            return None

        return {
            "status": "stale",
            "loaded_at": latest.loaded_at.isoformat(),
            "error": fallback_reason,
            "balances": latest.balances,
        }

    @classmethod
    def get_balances_snapshot(cls) -> dict:
        """Return current Comdirect balance snapshot."""
        return cls._snapshot

    @classmethod
    def should_render_balance_section(cls) -> bool:
        """Whether the dashboard should render the account balances section."""
        return cls._snapshot.get("status") != "disabled-no-snapshot"


class ChartDataService:
    """Service for generating chart data."""

    @staticmethod
    async def get_chart_data(
        year: Optional[int] = None,
        categories: list[str] | None = None,
        category_type: str | None = None,
    ) -> dict:
        """
        Get chart data aggregated by month and category.

        Args:
            year: Filter by year (optional, defaults to all years)
            categories: List of minor categories to include (optional,
                if empty includes all)
            category_type: Category grouping type (major or minor)

        Returns:
            Dict with months, categories, data structure, and available years
        """
        # Get all transactions once and reuse for all derived views.
        all_transactions = await Transaction.find_all().to_list()
        transactions = all_transactions

        category_field = (
            "major_category" if category_type == "major" else "minor_category"
        )

        # Store unfiltered transactions for determining month range
        unfiltered_transactions = [
            t for t in all_transactions if not should_exclude_transaction(t)
        ]

        # Filter by year if provided
        if year:
            transactions = [t for t in transactions if t.date.year == year]

        # Transfers move money without spending it.
        transactions = [t for t in transactions if not should_exclude_transaction(t)]

        # Get available categories
        available_categories = set()
        for t in transactions:
            available_categories.add(getattr(t, category_field, None) or "Other")

        # If specific categories are requested, filter to those
        if categories:
            filter_categories = set(categories)
            transactions = [
                t
                for t in transactions
                if (getattr(t, category_field, None) or "Other") in filter_categories
            ]
            available_categories = filter_categories

        # Group data by month and category
        data_by_month = group_by_month_and_category_field(
            transactions,
            category_field,
        )

        # Get all available years from transactions
        available_years = sorted(set(t.date.year for t in all_transactions))

        # Format response
        months = sorted(data_by_month.keys())

        # Ensure all months are shown
        if year:
            months = generate_all_months_in_year(year)
        elif unfiltered_transactions:
            min_date = min(t.date for t in unfiltered_transactions)
            max_date = max(t.date for t in unfiltered_transactions)
            months = generate_month_range(min_date.date(), max_date.date())

        category_list = sorted(available_categories)

        response_data = {
            "months": months,
            "categories": category_list,
            "data": {},
            "available_years": available_years,
        }

        # Build data structure for each category
        for category in category_list:
            response_data["data"][category] = [
                data_by_month[month].get(category, 0) for month in months
            ]

        return response_data

    @staticmethod
    async def get_cashflow_data(year: Optional[int] = None) -> dict:
        """
        Get net cashflow data aggregated by month.
        Excludes transfers, which move money without earning or spending it.

        Args:
            year: Filter by year (optional, defaults to all years)

        Returns:
            Dict with months, data values, total, and available years
        """
        # Get all transactions
        all_transactions = await Transaction.find_all().to_list()

        # Store unfiltered transactions for determining month range
        unfiltered_transactions = [
            t for t in all_transactions if not should_exclude_transaction(t)
        ]

        # Filter by year if provided
        if year:
            all_transactions = [t for t in all_transactions if t.date.year == year]

        # Transfers move money without earning or spending it.
        all_transactions = [
            t for t in all_transactions if not should_exclude_transaction(t)
        ]

        # Group by month and sum amounts
        data_by_month = group_by_month(all_transactions)

        # Get all available years from unfiltered transactions
        available_years = sorted(set(t.date.year for t in unfiltered_transactions))

        # Format response
        months = sorted(data_by_month.keys())

        # Ensure all months are shown
        if year:
            months = generate_all_months_in_year(year)
        elif unfiltered_transactions:
            min_date = min(t.date for t in unfiltered_transactions)
            max_date = max(t.date for t in unfiltered_transactions)
            months = generate_month_range(min_date.date(), max_date.date())

        # Build data array
        cashflow_data = [data_by_month.get(month, 0) for month in months]

        # Calculate total
        total_cashflow = sum(cashflow_data)

        response_data = {
            "months": months,
            "data": cashflow_data,
            "total": total_cashflow,
            "available_years": available_years,
        }

        return response_data

    @staticmethod
    def _month_bounds(month: str) -> tuple[datetime, datetime]:
        """
        The half-open datetime range covering a ``YYYY-MM`` month key.

        Returns:
            Start of the month and start of the following one.

        Raises:
            ValueError: If ``month`` is not a ``YYYY-MM`` key.
        """
        try:
            year, index = (int(part) for part in month.split("-"))
            start = datetime(year, index, 1)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{month!r} is not a YYYY-MM month") from error
        # December rolls into January of the next year; every other month just
        # steps its index.
        end = datetime(year + 1, 1, 1) if index == 12 else datetime(year, index + 1, 1)
        return start, end

    @staticmethod
    def _period_bounds(
        month: Optional[str], year: Optional[int]
    ) -> tuple[Optional[datetime], Optional[datetime]]:
        """
        The half-open range a detail request covers, or ``(None, None)``.

        A month is more specific than a year and wins when both are given —
        the month already names one. ``(None, None)`` means all of history,
        which is what the dashboard's "All years" asks for.
        """
        if month:
            return ChartDataService._month_bounds(month)
        if year:
            return datetime(year, 1, 1), datetime(year + 1, 1, 1)
        return None, None

    @staticmethod
    async def get_transaction_detail(
        month: Optional[str] = None,
        year: Optional[int] = None,
        categories: Optional[list[str]] = None,
        category_type: Optional[str] = None,
        limit: int = TOP_DETAIL_TRANSACTIONS,
    ) -> dict:
        """
        The transactions behind one thing the dashboard drew.

        Every plot resolves a click to the same two questions — which period,
        and which categories — so one endpoint answers for all of them: a bar
        of the monthly chart asks for a month, a slice of a breakdown asks for
        a category over the selected year, and the spectrum asks for a major
        category over that same year.

        Categories are a list because not every slice stands for one of them.
        A breakdown collapses its long tail into a single "N smaller
        categories" wedge, and that wedge is worth opening too — it is simply
        a click that names several categories instead of one.

        Both directions come back separately rather than as one "biggest
        movements" ranking. What was clicked is a *net* sum, so a refund filed
        under an expense category belongs to it just as much as the spending
        does, and merging the two would let one large refund crowd out the
        payments it offsets.

        Args:
            month: Month key in ``YYYY-MM`` format, or None.
            year: Year to cover, or None for all of history. Ignored when
                ``month`` is given.
            categories: Categories to narrow to, or None for all of them.
            category_type: Whether ``categories`` names major or minor
                categories. Must match the granularity the plot was drawn at.
            limit: How many transactions to return per direction.

        Returns:
            Dict echoing the scope, with the net total and an ``incoming`` and
            ``outgoing`` side, each holding its top transactions, its own
            total and how much of that total the returned rows account for.

        Raises:
            ValueError: If ``month`` is given and is not a ``YYYY-MM`` key.
        """
        start, end = ChartDataService._period_bounds(month, year)
        category_field = (
            "major_category" if category_type == "major" else "minor_category"
        )

        # Reading the whole collection is only right when the whole of history
        # was asked for; any narrower scope is a range query.
        if start is None:
            transactions = await Transaction.find_all().to_list()
        else:
            transactions = await Transaction.find(
                Transaction.date >= start, Transaction.date < end
            ).to_list()

        wanted = set(categories) if categories else None

        # The same exclusion the charts use, so the detail adds up to the
        # thing it was opened from.
        rows = [
            transaction
            for transaction in transactions
            if not should_exclude_transaction(transaction)
            and (
                wanted is None
                or (getattr(transaction, category_field, None) or "Other") in wanted
            )
        ]

        incoming = sorted(
            (t for t in rows if t.amount > 0), key=lambda t: (-t.amount, t.date)
        )
        outgoing = sorted(
            (t for t in rows if t.amount < 0), key=lambda t: (t.amount, t.date)
        )

        return {
            "month": month,
            "year": year,
            "categories": list(categories) if categories else None,
            "category_type": "major" if category_type == "major" else "minor",
            "net": sum(t.amount for t in rows),
            "count": len(rows),
            "incoming": _summarize_side(incoming, limit),
            "outgoing": _summarize_side(outgoing, limit),
        }


class CategorizationService:
    """Service for transaction categorization operations."""

    @staticmethod
    async def get_uncategorized_transaction(skip: int = 0) -> Optional[Transaction]:
        """
        Get an uncategorized transaction, most recent first.

        Args:
            skip: How many transactions to step past, so the review UI can move
                to the next item without categorizing the current one.
        """
        results = await (
            Transaction.find(Transaction.minor_category == "Uncategorized")
            .sort("-date")
            .skip(max(0, skip))
            .limit(1)
            .to_list()
        )
        return results[0] if results else None

    @staticmethod
    async def get_uncategorized_count() -> int:
        """Get the count of uncategorized transactions."""
        return await Transaction.find(
            Transaction.minor_category == "Uncategorized"
        ).count()

    @staticmethod
    async def recompute_all_categories(
        categorizer, only_uncategorized: bool = False
    ) -> None:
        """
        Recompute categories for transactions.

        Args:
            categorizer: Categorizer instance with rules
            only_uncategorized: Only re-evaluate transactions still marked as
                Other/Uncategorized
        """
        try:
            transactions = await Transaction.find_all().to_list()
            transactions_to_process = [
                transaction
                for transaction in transactions
                if not only_uncategorized or categorizer._is_uncategorized(transaction)
            ]
            categorizer.categorize(
                transactions=transactions_to_process,
                only_uncategorized=False,
            )

            for transaction in transactions_to_process:
                await transaction.save()
            logger.info(
                "Recomputed categories for %s transactions",
                len(transactions_to_process),
            )
        except Exception as e:
            logger.error(f"Failed to recompute categories: {e}")
            raise


class ReviewService:
    """The queue of transactions the classifier would not decide."""

    @staticmethod
    async def queue(limit: int = 100, skip: int = 0) -> list[Transaction]:
        """Transactions awaiting a human decision, least confident first."""
        return await (
            Transaction.find(Transaction.minor_category == MINOR_NEEDS_REVIEW)
            .sort("confidence", "-date")
            .skip(max(0, skip))
            .limit(limit)
            .to_list()
        )

    @staticmethod
    async def count() -> int:
        return await Transaction.find(
            Transaction.minor_category == MINOR_NEEDS_REVIEW
        ).count()

    @staticmethod
    async def paypal_context(transactions: list) -> dict:
        """
        What PayPal knew about each of these transactions, by transaction id.

        A PayPal booking line is the hardest thing in the queue to decide:
        Comdirect prints no payee on it, so once the payee has been recovered
        the rest of what PayPal recorded — who the two sides were, and
        whatever the payer wrote on the payment — is the only context a person
        has. A payment between two people frequently carries nothing else.

        Only transactions already matched to a settlement appear; the rest are
        ordinary lines with nothing to add.
        """
        from app.paypal import settlement_details

        by_code = {
            transaction.paypal_settlement_code: transaction
            for transaction in transactions
            if getattr(transaction, "paypal_settlement_code", None)
        }
        if not by_code:
            return {}

        found = await settlement_details(by_code)
        return {str(by_code[code].id): settlement for code, settlement in found.items()}

    @staticmethod
    async def correct(
        transaction_id: str, major: str, minor: str
    ) -> tuple[Transaction, bool]:
        """
        File a transaction by hand and teach the merchant cache.

        The correction is the point: it settles this transaction *and* every
        future one from the same merchant *running the same way*. The cache
        entry is written as ``user``, which the classifier is not allowed to
        overwrite.

        Returns:
            The updated transaction and whether the merchant cache was
            written — it is not, when the booking line holds no merchant to
            key on.
        """
        from app.classification.service import direction_of, remember_merchant

        transaction = await Transaction.get(PydanticObjectId(transaction_id))
        if transaction is None:
            raise ValueError(f"No transaction {transaction_id}")

        # A hand correction is not coerced. Silently turning an invalid pair
        # into Needs Review would file the transaction as unreviewed *and*
        # teach the merchant cache that answer as if a person had chosen it —
        # a malformed form post would then poison every future transaction
        # from that merchant.
        if not is_valid_pair(major, minor):
            raise ValueError(f"{major!r} / {minor!r} is not a category")

        transaction.major_category = major
        transaction.minor_category = minor
        transaction.flow_type = flow_type_for(major)
        transaction.confidence = 1.0
        transaction.reason = "Categorised by hand."
        transaction.classification_source = SOURCE_USER
        await transaction.save()

        merchant = normalize_merchant(transaction.booking_text, transaction.process)
        if merchant:
            # Taught for this transaction's direction only. Correcting a
            # payment *to* someone must not overwrite what was learned from
            # the payments received *from* them, which mean something else.
            await remember_merchant(
                merchant,
                major,
                minor,
                SOURCE_USER,
                direction_of(transaction.amount),
            )
        return transaction, bool(merchant)


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
