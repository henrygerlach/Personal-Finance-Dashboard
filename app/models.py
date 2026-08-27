"""
Data models for the application using Beanie ODM.
"""

from datetime import datetime
from typing import Optional

from beanie import Document, Indexed
from pydantic import Field
from pymongo import ASCENDING, IndexModel

# Where a transaction's category came from. Recorded per transaction so a
# result can always be traced back and, if needed, recomputed.
SOURCE_LEGACY_RULES = "legacy-rules"  # carried over from the hand-written rules
SOURCE_PENDING = "pending"  # provisional category, awaiting classification
SOURCE_LLM = "llm"  # returned by the classifier
SOURCE_USER = "user"  # corrected by hand; outranks everything else
SOURCE_FALLBACK = "fallback"  # no merchant in the booking text; see normalization

# Which way the money ran. A remembered category answers "what was this for",
# and the same merchant can produce both directions with opposite meanings —
# maintenance received from a person, and a payment sent to that same person.
# The direction is therefore part of what a merchant entry remembers.
DIRECTION_IN = "in"  # an incoming amount
DIRECTION_OUT = "out"  # an outgoing amount

DIRECTIONS = frozenset({DIRECTION_IN, DIRECTION_OUT})

CLASSIFICATION_SOURCES = frozenset(
    {
        SOURCE_LEGACY_RULES,
        SOURCE_PENDING,
        SOURCE_LLM,
        SOURCE_USER,
        SOURCE_FALLBACK,
    }
)

# What a hand-made decision can say about a detected schedule. Detection reads
# a pattern out of the history and can only ever guess at two things: whether
# the pattern is a schedule at all, and whether it is still running. These are
# the answers to those two questions, and nothing else — an override says what
# the user knows that the dates cannot show.
OVERRIDE_DISMISSED = "dismissed"  # not a recurring payment; hide it
OVERRIDE_ACTIVE = "active"  # still running, whatever the last charge says
OVERRIDE_ENDED = "ended"  # stopped, whatever the last charge says

OVERRIDE_DECISIONS = frozenset({OVERRIDE_DISMISSED, OVERRIDE_ACTIVE, OVERRIDE_ENDED})


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
    paypal_counterparty: Optional[str] = Field(
        None,
        description=(
            "Merchant or person recovered from a PayPal export for a booking "
            "line whose payee Comdirect truncated away. Set by app.paypal; "
            "None means the line needed no help or none could be found."
        ),
    )
    paypal_settlement_code: Optional[str] = Field(
        None,
        description=(
            "PayPal transaction code of the settlement this line was matched "
            "to, so a recovered merchant can be audited back to its source."
        ),
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

    An entry is identified by the merchant *and* the direction it was learned
    from, because one merchant can mean two different things depending on
    which way the money ran: maintenance received from a person is income,
    while a payment sent to that same person is a transfer. One entry per
    merchant let the first direction seen answer for both, which filed
    outgoing payments as income. A merchant that only ever runs one way still
    keeps one entry — the lookup falls back across the direction when there is
    nothing better, and :func:`app.classification.service.entry_applies`
    decides whether that fallback can honestly answer.
    """

    merchant_key: str = Field(
        ...,
        description=(
            "Lookup key from app.normalization.merchant_key: the normalized "
            "name with spaces removed, so a line wrap in the middle of a word "
            "cannot split one merchant into two entries. Indexed as the "
            "prefix of merchant_key_direction rather than on its own."
        ),
    )
    display_name: str = Field(
        ...,
        description=(
            "The readable normalized name behind the key, for the review UI "
            "and the classification prompt."
        ),
    )
    direction: str = Field(
        DIRECTION_OUT,
        description=(
            f"'{DIRECTION_IN}' or '{DIRECTION_OUT}': the direction of the "
            "transactions this entry speaks for. Part of the identity of an "
            "entry, so a merchant that runs both ways can hold one answer "
            "per direction."
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
        indexes = [
            # Unique on the pair, not on the merchant alone: two entries for
            # one merchant are the point. The compound index also serves a
            # lookup on merchant_key alone, since that is its prefix.
            IndexModel(
                [("merchant_key", ASCENDING), ("direction", ASCENDING)],
                unique=True,
                name="merchant_key_direction",
            )
        ]


class PayPalRecord(Document):
    """
    One row of a PayPal activity export, stored as it was read.

    The collection is the raw import, not an interpretation of it: the meaning
    of a row — whether it settles against the bank, which merchant stands
    behind it — is worked out in app.paypal.ledger every time the ledger is
    built. Keeping the two apart means improving that logic needs no
    re-import, and re-importing an overlapping export changes nothing.

    ``code`` is PayPal's own transaction code and is unique, which is what
    makes repeated imports of overlapping downloads idempotent.
    """

    code: Indexed(str, unique=True) = Field(
        ..., description="PayPal transaction code (Transaktionscode)."
    )
    date: datetime = Field(..., description="Date PayPal recorded the movement.")
    kind: str = Field(..., description="PayPal's transaction type, verbatim.")
    status: str = Field("", description="PayPal's status, verbatim.")
    name: str = Field("", description="Counterparty as PayPal recorded it.")
    currency: str = Field("", description="ISO currency code of the amount.")
    gross: float = Field(
        ...,
        description=(
            "Gross amount in PayPal's sign convention: negative leaves the "
            "PayPal balance, positive enters it."
        ),
    )
    related_code: str = Field(
        "",
        description=(
            "Transaction code this row belongs to. The thread the ledger "
            "pulls to find a merchant for a settlement."
        ),
    )
    item_title: str = Field("", description="What was bought, when recorded.")
    sender: str = Field(
        "",
        description=(
            "Address the payment was sent from, as PayPal recorded it. "
            "Context for a person reviewing a transaction, never evidence "
            "for a match. Empty on rows where PayPal did not expose it, and "
            "on every row imported before this field existed."
        ),
    )
    receiver: str = Field(
        "", description="Address the payment was sent to. See ``sender``."
    )
    subject: str = Field(
        "", description="Subject line the payer gave the payment, if any."
    )
    note: str = Field(
        "",
        description=(
            "Message the payer wrote on the payment. Often the only thing "
            "saying what a payment between two people was for."
        ),
    )
    imported_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When this row was first stored.",
    )

    class Settings:
        name = "paypal_records"


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


class RecurringOverride(Document):
    """
    A hand-made decision about one detected schedule.

    Detection is a heuristic over dates and amounts, so it is wrong
    occasionally in both directions: three bakery visits a month apart are
    reported as a subscription, and a schedule paused over the summer is
    reported as ended. Neither can be fixed by tuning a threshold without
    breaking the other, so the user gets the last word.

    An override is keyed on what actually makes a run distinct — the merchant,
    the billing period, and the amount — rather than on any id belonging to
    the run. Runs are recomputed from scratch on every page load and carry no
    identity across that, so an override keyed on one would quietly stop
    applying the moment a new charge moved the run's boundaries. Those three
    fields are the same triple the detector itself groups by: a merchant's
    charges are split into amount clusters and each cluster is examined per
    period, so two schedules the detector keeps apart get their own overrides,
    while a price rise — which stays inside one run precisely because it is
    within the amount tolerance — keeps the override it already had.
    """

    merchant_key: str = Field(
        ...,
        description=(
            "Lookup key from app.normalization.merchant_key for the run's "
            "canonical merchant name."
        ),
    )
    display_name: str = Field(
        ..., description="The readable merchant name, for listing the override."
    )
    period: str = Field(..., description="The run's billing period, e.g. 'monthly'.")
    amount: float = Field(
        ...,
        description=(
            "The run's price when the decision was made. Matched within the "
            "detector's own amount tolerance, so the override survives the "
            "ordinary drift of a variable bill and a later price rise."
        ),
    )
    decision: str = Field(..., description=f"One of {sorted(OVERRIDE_DECISIONS)}.")
    decided_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="When this decision was last written.",
    )

    class Settings:
        name = "recurring_overrides"
        indexes = [
            # Not unique: one merchant can run several schedules at the same
            # period, told apart by amount, and an amount match is a
            # tolerance rather than an equality. The service enforces one
            # override per schedule when it writes.
            IndexModel(
                [("merchant_key", ASCENDING), ("period", ASCENDING)],
                name="merchant_period",
            )
        ]
