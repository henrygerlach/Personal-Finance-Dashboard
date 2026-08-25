"""
Business logic services for categorization and analytics.
"""
import asyncio
from datetime import datetime
from typing import Optional

from app.config import get_logger
from app.models import ComdirectBalanceSnapshot, Transaction
from app.utils import (
    generate_all_months_in_year,
    generate_month_range,
    group_by_month,
    group_by_month_and_category_field,
    should_exclude_transaction,
)

logger = get_logger(__name__)


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
            t
            for t in all_transactions
            if not should_exclude_transaction(t.minor_category)
        ]

        # Filter by year if provided
        if year:
            transactions = [t for t in transactions if t.date.year == year]

        # Exclude Internal transfers
        transactions = [
            t for t in transactions if not should_exclude_transaction(t.minor_category)
        ]

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
        Excludes Uncategorized and Transfers Internal transactions.

        Args:
            year: Filter by year (optional, defaults to all years)

        Returns:
            Dict with months, data values, total, and available years
        """
        # Get all transactions
        all_transactions = await Transaction.find_all().to_list()

        # Store unfiltered transactions for determining month range
        unfiltered_transactions = [
            t
            for t in all_transactions
            if not should_exclude_transaction(t.minor_category)
        ]

        # Filter by year if provided
        if year:
            all_transactions = [t for t in all_transactions if t.date.year == year]

        # Exclude Uncategorized and Internal transfers
        all_transactions = [
            t
            for t in all_transactions
            if not should_exclude_transaction(t.minor_category)
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
