"""
Tests for the utility functions.
"""
from datetime import date, datetime
from types import SimpleNamespace

from app.utils import (
    extract_month_key,
    generate_all_months_in_year,
    generate_month_range,
    group_by_month,
    group_by_month_and_category,
    should_exclude_transaction,
)


def test_generate_month_range():
    """Test month range generation."""
    min_date = date(2024, 1, 15)
    max_date = date(2024, 3, 20)

    months = generate_month_range(min_date, max_date)
    assert months == ["2024-01", "2024-02", "2024-03"]


def test_generate_month_range_cross_year():
    """Test month range generation across year boundary."""
    min_date = date(2023, 11, 1)
    max_date = date(2024, 2, 28)

    months = generate_month_range(min_date, max_date)
    assert months == ["2023-11", "2023-12", "2024-01", "2024-02"]


def test_generate_all_months_in_year():
    """Test all months generation for a year."""
    months = generate_all_months_in_year(2024)
    assert len(months) == 12
    assert months[0] == "2024-01"
    assert months[-1] == "2024-12"


def test_extract_month_key():
    """Test month key extraction."""
    dt = datetime(2024, 3, 15)
    assert extract_month_key(dt) == "2024-03"


def test_should_exclude_transaction():
    """Test transaction exclusion logic."""
    assert should_exclude_transaction("Uncategorized")
    assert should_exclude_transaction("Internal")
    assert not should_exclude_transaction("Groceries")


def test_group_by_month_and_category():
    """Test grouping transactions by month and category."""
    transactions = [
        SimpleNamespace(
            hash="1",
            date=datetime(2024, 1, 15),
            amount=50.0,
            booking_text="test",
            minor_category="Groceries",
        ),
        SimpleNamespace(
            hash="2",
            date=datetime(2024, 1, 20),
            amount=30.0,
            booking_text="test",
            minor_category="Groceries",
        ),
        SimpleNamespace(
            hash="3",
            date=datetime(2024, 2, 10),
            amount=20.0,
            booking_text="test",
            minor_category="Entertainment",
        ),
    ]

    result = group_by_month_and_category(transactions)
    assert result["2024-01"]["Groceries"] == 80.0
    assert result["2024-02"]["Entertainment"] == 20.0


def test_group_by_month():
    """Test grouping transactions by month."""
    transactions = [
        SimpleNamespace(
            hash="1",
            date=datetime(2024, 1, 15),
            amount=50.0,
            booking_text="test",
        ),
        SimpleNamespace(
            hash="2",
            date=datetime(2024, 1, 20),
            amount=30.0,
            booking_text="test",
        ),
        SimpleNamespace(
            hash="3",
            date=datetime(2024, 2, 10),
            amount=20.0,
            booking_text="test",
        ),
    ]

    result = group_by_month(transactions)
    assert result["2024-01"] == 80.0
    assert result["2024-02"] == 20.0
