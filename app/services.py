"""
Business logic services for categorization and analytics.
"""

import asyncio
from datetime import datetime
from typing import Optional

from beanie import PydanticObjectId

from app.categories import MINOR_NEEDS_REVIEW, flow_type_for, is_valid_pair
from app.config import get_logger
from app.models import SOURCE_USER, ComdirectBalanceSnapshot, Transaction
from app.normalization import normalize_merchant
from app.recurrence import find_recurring, recurring_transaction_ids
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
        category: Optional[str] = None,
        category_type: Optional[str] = None,
        limit: int = TOP_DETAIL_TRANSACTIONS,
    ) -> dict:
        """
        The transactions behind one thing the dashboard drew.

        Every plot resolves a click to the same two questions — which period,
        and which category — so one endpoint answers for all of them: a bar of
        the monthly chart asks for a month, a slice of a breakdown asks for a
        category over the selected year, and the spectrum asks for a major
        category over that same year.

        Both directions come back separately rather than as one "biggest
        movements" ranking. What was clicked is a *net* sum, so a refund filed
        under an expense category belongs to it just as much as the spending
        does, and merging the two would let one large refund crowd out the
        payments it offsets.

        Args:
            month: Month key in ``YYYY-MM`` format, or None.
            year: Year to cover, or None for all of history. Ignored when
                ``month`` is given.
            category: Category to narrow to, or None for every category.
            category_type: Whether ``category`` names a major or a minor
                category. Must match the granularity the plot was drawn at.
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

        # The same exclusion the charts use, so the detail adds up to the
        # thing it was opened from.
        rows = [
            transaction
            for transaction in transactions
            if not should_exclude_transaction(transaction)
            and (
                category is None
                or (getattr(transaction, category_field, None) or "Other") == category
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
            "category": category,
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
    async def correct(
        transaction_id: str, major: str, minor: str
    ) -> tuple[Transaction, bool]:
        """
        File a transaction by hand and teach the merchant cache.

        The correction is the point: it settles this transaction *and* every
        future one from the same merchant. The cache entry is written as
        ``user``, which the classifier is not allowed to overwrite.

        Returns:
            The updated transaction and whether the merchant cache was
            written — it is not, when the booking line holds no merchant to
            key on.
        """
        from app.classification.service import remember_merchant

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
            await remember_merchant(merchant, major, minor, SOURCE_USER)
        return transaction, bool(merchant)


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
        recurring = recurring_transaction_ids(runs)

        for transaction in transactions:
            should_be = str(transaction.id) in recurring
            if transaction.is_recurring != should_be:
                transaction.is_recurring = should_be
                await transaction.save()

        logger.info(
            "Recurring detection: %s schedules covering %s transactions",
            len(runs),
            len(recurring),
        )
        return runs

    @staticmethod
    async def overview() -> dict:
        """
        The recurring payments, grouped by schedule, with a monthly total.

        Quarterly and yearly charges are shown at their monthly equivalent so
        the total means something: a 90 EUR annual insurance is 7.50 a month
        next to a 4.99 subscription.
        """
        transactions = await Transaction.find_all().to_list()
        runs = find_recurring(transactions)
        per_month = {"monthly": 1.0, "quarterly": 3.0, "yearly": 12.0}
        by_id = {str(t.id): t for t in transactions}

        items = []
        for run in runs:
            latest = max(
                (by_id[tid] for tid in run.transaction_ids if tid in by_id),
                key=lambda t: t.date,
                default=None,
            )
            if latest is None:
                continue
            items.append(
                {
                    "merchant": run.merchant,
                    "period": run.period,
                    "occurrences": run.occurrences,
                    "amount": round(run.median_amount, 2),
                    "monthly_equivalent": round(
                        run.median_amount / per_month[run.period], 2
                    ),
                    "major": latest.major_category,
                    "minor": latest.minor_category,
                    "flow_type": latest.flow_type,
                    "last_seen": latest.date.strftime("%Y-%m-%d"),
                }
            )

        # Transfers move money without earning or spending it, so they stay
        # out of both totals — a monthly savings plan is not an expense and a
        # standing order between own accounts is not income. They are still
        # listed, because a 500-a-month transfer is worth seeing.
        spending = [i for i in items if i["flow_type"] != "transfer"]
        transfers = [i for i in items if i["flow_type"] == "transfer"]
        return {
            "items": items,
            "monthly_expense": round(
                sum(
                    i["monthly_equivalent"]
                    for i in spending
                    if i["monthly_equivalent"] < 0
                ),
                2,
            ),
            "monthly_income": round(
                sum(
                    i["monthly_equivalent"]
                    for i in spending
                    if i["monthly_equivalent"] > 0
                ),
                2,
            ),
            "monthly_transfer": round(
                sum(i["monthly_equivalent"] for i in transfers), 2
            ),
            "transfer_count": len(transfers),
            "count": len(items),
        }
