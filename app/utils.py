"""
Utility functions and helpers.
"""

from collections import defaultdict
from datetime import date, datetime
from typing import DefaultDict

# Constants for category filtering
INTERNAL_CATEGORIES = {"Uncategorized", "Internal"}

# Transfers move money without earning or spending it, so they must stay out of
# every expense total, expense chart and monthly comparison.
TRANSFER_FLOW = "transfer"

# Time constants
MONTHS_PER_YEAR = 12


def generate_month_range(min_date: date, max_date: date) -> list[str]:
    """
    Generate a list of month strings in YYYY-MM format.

    Args:
        min_date: Start date
        max_date: End date

    Returns:
        List of month strings from min_date to max_date
    """
    months = []
    current_year = min_date.year
    current_month = min_date.month

    while (current_year, current_month) <= (max_date.year, max_date.month):
        months.append(f"{current_year:04d}-{current_month:02d}")
        current_month += 1
        if current_month > MONTHS_PER_YEAR:
            current_month = 1
            current_year += 1

    return months


def generate_all_months_in_year(year: int) -> list[str]:
    """
    Generate a list of all 12 months for a given year.

    Args:
        year: The year

    Returns:
        List of month strings [YYYY-01 through YYYY-12]
    """
    return [f"{year:04d}-{month:02d}" for month in range(1, MONTHS_PER_YEAR + 1)]


def extract_month_key(date_obj: datetime) -> str:
    """
    Extract month key in YYYY-MM format from a datetime object.

    Args:
        date_obj: Datetime object

    Returns:
        Month string in YYYY-MM format
    """
    return date_obj.strftime("%Y-%m")


def should_exclude_transaction(transaction) -> bool:
    """
    Whether a transaction must stay out of income and expense analysis.

    Decided by ``flow_type``, which is derived from the major category rather
    than guessed. The previous version matched two hard-coded minor category
    names, and that let real money through: transfers to other people counted
    as income, and securities purchases and cash withdrawals counted as
    spending — about 8,800 EUR and 12,700 EUR respectively in the live data.

    A transaction from before the taxonomy migration has no flow type. Those
    fall back to the old name check so the dashboard keeps working on a
    half-migrated database.

    Args:
        transaction: A transaction, or a bare minor-category name for callers
            that only have that.
    """
    if isinstance(transaction, str):
        return transaction in INTERNAL_CATEGORIES

    flow_type = getattr(transaction, "flow_type", None)
    if flow_type:
        return flow_type == TRANSFER_FLOW

    minor = getattr(transaction, "minor_category", None)
    return minor in INTERNAL_CATEGORIES


def group_by_month_and_category(
    transactions: list,
) -> DefaultDict[str, DefaultDict[str, float]]:
    """
    Group transactions by month and category, summing amounts.

    Args:
        transactions: List of Transaction objects

    Returns:
        Nested dict with structure: {month_key: {category: total_amount}}
    """
    data = defaultdict(lambda: defaultdict(float))
    for transaction in transactions:
        month_key = extract_month_key(transaction.date)
        category = transaction.minor_category
        data[month_key][category] += transaction.amount
    return data


def group_by_month_and_category_field(
    transactions: list,
    category_field: str,
) -> DefaultDict[str, DefaultDict[str, float]]:
    """
    Group transactions by month and a category field, summing amounts.

    Args:
        transactions: List of Transaction objects
        category_field: Attribute name to group by (e.g. major_category)

    Returns:
        Nested dict with structure: {month_key: {category: total_amount}}
    """
    data = defaultdict(lambda: defaultdict(float))
    for transaction in transactions:
        month_key = extract_month_key(transaction.date)
        category = getattr(transaction, category_field, None) or "Other"
        data[month_key][category] += transaction.amount
    return data


def group_by_month(transactions: list) -> DefaultDict[str, float]:
    """
    Group transactions by month, summing amounts.

    Args:
        transactions: List of Transaction objects

    Returns:
        Dict with structure: {month_key: total_amount}
    """
    data = defaultdict(float)
    for transaction in transactions:
        month_key = extract_month_key(transaction.date)
        data[month_key] += transaction.amount
    return data
