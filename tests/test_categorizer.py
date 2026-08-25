"""
Tests for the categorizer module.
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import services
from app.categorizer import Categorizer


@pytest.fixture
def sample_rules():
    """Create sample categorization rules."""
    return [
        SimpleNamespace(
            substring="REWE",
            major="Food",
            minor="Groceries",
            process=None,
        ),
        SimpleNamespace(
            substring="NETFLIX",
            major="Entertainment",
            minor="Streaming",
            process=None,
        ),
    ]


def test_categorizer_basic_matching(sample_rules):
    """Test basic substring matching."""
    categorizer = Categorizer(sample_rules)

    transaction = SimpleNamespace(
        hash="test123",
        date=datetime.now(),
        amount=50.0,
        booking_text="REWE DACHAU MARKET",
        process=None,
    )

    major, minor = categorizer.apply_rules(transaction)
    assert major == "Food"
    assert minor == "Groceries"


def test_categorizer_case_insensitive(sample_rules):
    """Test that matching is case-insensitive."""
    categorizer = Categorizer(sample_rules)

    transaction = SimpleNamespace(
        hash="test456",
        date=datetime.now(),
        amount=15.99,
        booking_text="netflix subscription",
        process=None,
    )

    major, minor = categorizer.apply_rules(transaction)
    assert major == "Entertainment"
    assert minor == "Streaming"


def test_categorizer_no_match(sample_rules):
    """Test default category when no rule matches."""
    categorizer = Categorizer(sample_rules)

    transaction = SimpleNamespace(
        hash="test789",
        date=datetime.now(),
        amount=100.0,
        booking_text="UNKNOWN MERCHANT",
        process=None,
    )

    major, minor = categorizer.apply_rules(transaction)
    assert major == "Other"
    assert minor == "Uncategorized"


def test_categorizer_uses_rule_order_when_rules_overlap():
    """Test that the first matching rule in the provided list wins."""
    rules = [
        SimpleNamespace(
            substring="REWE",
            major="Food",
            minor="Groceries",
            process=None,
        ),
        SimpleNamespace(
            substring="REWE",
            major="Other",
            minor="Miscellaneous",
            process=None,
        ),
    ]

    categorizer = Categorizer(rules)

    transaction = SimpleNamespace(
        hash="test999",
        date=datetime.now(),
        amount=50.0,
        booking_text="REWE DACHAU MARKET",
        process=None,
    )

    major, minor = categorizer.apply_rules(transaction)
    assert major == "Food"
    assert minor == "Groceries"


def test_recompute_all_categories_only_updates_uncategorized(monkeypatch):
    """Only transactions that are still uncategorized should be updated."""

    saved_transactions = []

    def make_save(transaction):
        async def save():
            saved_transactions.append(transaction)
            return None

        return save

    already_categorized = SimpleNamespace(
        booking_text="REWE MARKET",
        process=None,
        major_category="Food",
        minor_category="Groceries",
    )
    uncategorized = SimpleNamespace(
        booking_text="NETFLIX SUBSCRIPTION",
        process=None,
        major_category="Other",
        minor_category="Uncategorized",
    )

    already_categorized.save = make_save(already_categorized)
    uncategorized.save = make_save(uncategorized)

    class DummyQuery:
        def __init__(self, transactions):
            self.transactions = transactions

        async def to_list(self):
            return self.transactions

    class DummyTransaction:
        @classmethod
        def find_all(cls):
            return DummyQuery([already_categorized, uncategorized])

    monkeypatch.setattr(services, "Transaction", DummyTransaction)

    categorizer = Categorizer(
        [
            SimpleNamespace(
                substring="NETFLIX",
                major="Entertainment",
                minor="Streaming",
                process=None,
            )
        ]
    )

    asyncio.run(
        services.CategorizationService.recompute_all_categories(
            categorizer,
            only_uncategorized=True,
        )
    )

    assert already_categorized.major_category == "Food"
    assert already_categorized.minor_category == "Groceries"
    assert uncategorized.major_category == "Entertainment"
    assert uncategorized.minor_category == "Streaming"
    assert uncategorized in saved_transactions
    assert already_categorized not in saved_transactions
