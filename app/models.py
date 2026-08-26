"""
Data models for the application using Beanie ODM.
"""

from datetime import datetime
from typing import Optional

from beanie import Document, Indexed
from pydantic import Field

# Where a transaction's category came from. Recorded per transaction so a
# result can always be traced back and, if needed, recomputed.
SOURCE_LEGACY_RULES = "legacy-rules"  # carried over from the hand-written rules
SOURCE_PENDING = "pending"  # provisional category, awaiting classification
SOURCE_LLM = "llm"  # returned by the classifier
SOURCE_USER = "user"  # corrected by hand; outranks everything else
SOURCE_FALLBACK = "fallback"  # no merchant in the booking text; see normalization

CLASSIFICATION_SOURCES = frozenset(
    {
        SOURCE_LEGACY_RULES,
        SOURCE_PENDING,
        SOURCE_LLM,
        SOURCE_USER,
        SOURCE_FALLBACK,
    }
)


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
    flow_type: Optional[str] = Field(
        None,
        description=(
            "income, expense or transfer. Derived from the major category by "
            "app.categories.flow_type_for, never guessed by the classifier. "
            "None means the transaction predates the migration."
        ),
    )
    is_recurring: bool = Field(
        False,
        description=(
            "Whether this payment repeats on a regular schedule. Detected from "
            "the transaction history, not set by the classifier."
        ),
    )
    confidence: Optional[float] = Field(
        None,
        description="Classifier confidence between 0 and 1. None if not classified.",
    )
    reason: Optional[str] = Field(
        None, description="One sentence from the classifier explaining the category."
    )
    classification_source: Optional[str] = Field(
        None, description=f"One of {sorted(CLASSIFICATION_SOURCES)}."
    )
    legacy_major: Optional[str] = Field(
        None,
        description=(
            "Major category before the taxonomy migration. Kept so the "
            "migration stays reversible and can detect what it already ran on."
        ),
    )
    legacy_minor: Optional[str] = Field(
        None, description="Minor category before the taxonomy migration."
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


class MerchantCategory(Document):
    """
    A remembered category for one normalized merchant.

    Looked up before every classification call, so only merchants nobody has
    seen before reach the API. Entries written by hand outrank the classifier:
    once a category is corrected in the UI it becomes the answer for that
    merchant, and the model never overrides it.

    ``major`` and ``minor`` are the stored truth. ``category_code`` is kept
    alongside them for reading and for comparing against a model response, but
    it is derived from the hierarchy's order — reordering the hierarchy
    renumbers every code, so a stored code must never be resolved back into a
    category on its own.
    """

    merchant_key: Indexed(str, unique=True) = Field(
        ...,
        description=(
            "Lookup key from app.normalization.merchant_key: the normalized "
            "name with spaces removed, so a line wrap in the middle of a word "
            "cannot split one merchant into two entries."
        ),
    )
    display_name: str = Field(
        ...,
        description=(
            "The readable normalized name behind the key, for the review UI "
            "and the classification prompt."
        ),
    )
    major: str = Field(..., description="Major category for this merchant")
    minor: str = Field(..., description="Minor category for this merchant")
    category_code: Optional[str] = Field(
        None,
        description=(
            "The hierarchy code for major/minor at the time of writing. "
            "Derived, not authoritative."
        ),
    )
    source: str = Field(
        ...,
        description=(
            f"'{SOURCE_LLM}' or '{SOURCE_USER}'. A user entry is never "
            "overwritten by the classifier."
        ),
    )
    confidence: Optional[float] = Field(
        None,
        description=(
            "Confidence of the classification that produced this entry. "
            "None for entries carried over from the hand-made categories."
        ),
    )
    reason: Optional[str] = Field(
        None, description="The classifier's one-sentence justification, if it gave one."
    )
    hit_count: int = Field(0, description="How often this entry has answered a lookup")
    first_seen: datetime = Field(
        default_factory=datetime.utcnow,
        description="When this merchant was first written",
    )
    last_used: datetime = Field(
        default_factory=datetime.utcnow,
        description="When this entry last answered a lookup or was rewritten",
    )
    model_version: Optional[str] = Field(
        None,
        description=(
            "The model that produced this category, so entries from an older "
            "prompt or model can be found and re-run later."
        ),
    )

    class Settings:
        name = "merchant_categories"


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
