"""
What the dashboard's charts are drawn from.
"""


from datetime import datetime
from typing import Optional

from app.config import get_logger
from app.models import (
    Transaction,
)
from app.services.common import TOP_DETAIL_TRANSACTIONS, _summarize_side
from app.utils import (
    generate_all_months_in_year,
    generate_month_range,
    group_by_month,
    group_by_month_and_category_field,
    should_exclude_transaction,
)

logger = get_logger(__name__)

logger = get_logger(__name__)


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
