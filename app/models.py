"""
Data models for the application using Beanie ODM.
"""
from datetime import datetime
from typing import Optional

from beanie import Document, Indexed
from pydantic import Field


class Transaction(Document):
    """Bank transaction record."""

    hash: Indexed(str, unique=True) = Field(
        ...,
        description=(
            "MD5 hash of date, amount, booking_text, and process. "
            "Used for deduplication."
        ),
    )
    date: datetime = Field(..., description="Transaction date")
    amount: float = Field(..., description="Transaction amount in EUR")
    booking_text: str = Field(..., description="Transaction description/reference text")
    process: Optional[str] = Field(None, description="Process code from bank")
    major_category: Optional[str] = Field(
        "Other", description="Major category for analysis"
    )
    minor_category: Optional[str] = Field(
        "Uncategorized",
        description=(
            "Minor category for detailed analysis. Uncategorized if not yet "
            "manually assigned."
        ),
    )

    class Settings:
        name = "transactions"


class CategoryRule(Document):
    """Rule for automatic transaction categorization."""

    substring: str = Field(
        ..., description="Substring to match in booking text (case-insensitive)"
    )
    process: Optional[str] = Field(
        None,
        description=(
            "Process code to match (optional). If specified, both substring "
            "and process must match."
        ),
    )
    major: str = Field(..., description="Major category to assign")
    minor: str = Field(..., description="Minor category to assign")

    class Settings:
        name = "category_rules"


class ComdirectBalanceSnapshot(Document):
    """Persisted snapshot of Comdirect balances for fallback display."""

    loaded_at: datetime = Field(..., description="Timestamp when balances were loaded")
    balances: dict[str, float] = Field(
        default_factory=dict,
        description="Balance values by account/depot label",
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Timestamp when snapshot was stored",
    )

    class Settings:
        name = "comdirect_balance_snapshots"
