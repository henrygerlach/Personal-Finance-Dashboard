"""
Data models for the application using Beanie ODM.
"""

from datetime import datetime
from typing import Optional

from beanie import Document, Indexed, PydanticObjectId
from bson.errors import InvalidId
from pydantic import Field
from pymongo import ASCENDING, DESCENDING, IndexModel


def object_id(value: str) -> PydanticObjectId:
    """
    One id out of a URL or a form field.

    ``PydanticObjectId`` raises ``InvalidId``, which descends from
    ``BSONError`` and not from ``ValueError``, so the ``except ValueError``
    a route wraps its lookup in does not catch it and a mistyped id leaves as
    a 500 instead of the 404 or 422 the route meant. Ids reach the app from
    paths and form posts, where anything at all can arrive, so the conversion
    is funnelled through here and reported the way every other bad value is.

    Raises:
        ValueError: The value is not an object id.
    """
    try:
        return PydanticObjectId(value)
    except InvalidId:
        raise ValueError(f"{value!r} is not an id")


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

# What a hand-made decision can say about a transaction the trip suggester
# offered. There are only two answers, and the second one has to be storable:
# saying "no, that Amazon order in the trip window was not the holiday" is a
# decision, and it has to survive so the suggester stops asking.
TRIP_INCLUDED = "included"
TRIP_EXCLUDED = "excluded"

TRIP_DECISIONS = frozenset({TRIP_INCLUDED, TRIP_EXCLUDED})


class Transaction(Document):
    """Bank transaction record."""

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose ledger this transaction belongs to."
    )
    hash: Indexed(str) = Field(
        ...,
        description=(
            "MD5 hash of date, amount, booking_text, and process. "
            "Used for deduplication within one user's ledger — see the "
            "user_hash index, which is what actually enforces uniqueness."
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
        indexes = [
            # Dedup is per user: two people's banks can produce the same hash
            # by coincidence (the same amount, date and booking text), and
            # that must not stop the second import.
            IndexModel(
                [("user_id", ASCENDING), ("hash", ASCENDING)],
                unique=True,
                name="user_hash",
            ),
            # The badge counts on every page render are a query on this field,
            # and both are answered by counting index entries rather than by
            # reading the collection.
            IndexModel(
                [("user_id", ASCENDING), ("minor_category", ASCENDING)],
                name="user_minor_category",
            ),
            # Descending because every query that orders by date wants the
            # recent end first, and a single-field index is read either way.
            IndexModel(
                [("user_id", ASCENDING), ("date", DESCENDING)], name="user_date"
            ),
        ]


class CategoryRule(Document):
    """Rule for automatic transaction categorization."""

    user_id: Indexed(PydanticObjectId) = Field(..., description="Whose rule this is.")
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

    The cache is per user: two people's transactions from a merchant called
    "M" mean nothing about each other, and a correction one of them makes must
    not silently answer for the other's.
    """

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose merchant cache this entry belongs to."
    )
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
            # Unique on the triple, not on the merchant alone: two entries for
            # one merchant are the point, and two users can have entries for
            # the same merchant key without either one seeing the other's.
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("merchant_key", ASCENDING),
                    ("direction", ASCENDING),
                ],
                unique=True,
                name="user_merchant_key_direction",
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

    ``code`` is PayPal's own transaction code and is unique per user, which is
    what makes repeated imports of overlapping downloads idempotent.
    """

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose PayPal export this row came from."
    )
    code: Indexed(str) = Field(
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
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("code", ASCENDING)],
                unique=True,
                name="user_code",
            )
        ]


class ComdirectBalanceSnapshot(Document):
    """
    Persisted snapshot of one user's Comdirect balances.

    Credentials are now a per-user login (``ComdirectCredentials``) rather
    than process-wide configuration, so a snapshot is scoped the same way:
    two people's Comdirect balances must never answer for each other.
    """

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose Comdirect balances these are."
    )
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
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("loaded_at", DESCENDING)],
                name="user_loaded_at",
            )
        ]


class ComdirectCredentials(Document):
    """
    One user's Comdirect login, Fernet-encrypted (app/crypto.py).

    Every field is ciphertext and is only ever decrypted in memory to start a
    session; it never leaves the server and never reaches a template or a
    response body.
    """

    user_id: Indexed(PydanticObjectId, unique=True) = Field(
        ..., description="Whose Comdirect login this is."
    )
    client_id: str = Field(..., description="Fernet ciphertext of the client id.")
    client_secret: str = Field(
        ..., description="Fernet ciphertext of the client secret."
    )
    access_number: str = Field(
        ..., description="Fernet ciphertext of the Comdirect access number."
    )
    pin: str = Field(..., description="Fernet ciphertext of the PIN.")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="When this login was saved."
    )

    class Settings:
        name = "comdirect_credentials"


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

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose schedule this decision is about."
    )
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
                [
                    ("user_id", ASCENDING),
                    ("merchant_key", ASCENDING),
                    ("period", ASCENDING),
                ],
                name="user_merchant_period",
            )
        ]


class Trip(Document):
    """
    One journey, as a container for spending rather than a date filter.

    A trip's costs are not contained in its dates: flights are bought months
    ahead, a card payment abroad reaches the bank a week after the flight
    home, and somebody settles their share of the house in the autumn. So the
    dates stored here are not what decides membership — they only tell
    :mod:`app.trips` where to look for transactions worth asking about. What
    actually belongs to the trip is the set of :class:`TripAssignment`
    records, every one of them made by hand.
    """

    user_id: Indexed(PydanticObjectId) = Field(..., description="Whose trip this is.")
    name: str = Field(..., description="What the trip is called, e.g. 'Lisbon'.")
    destination: Optional[str] = Field(
        None, description="Where it went, for listing and for the reader's memory."
    )
    start_date: datetime = Field(..., description="First day of the trip, inclusive.")
    end_date: datetime = Field(..., description="Last day of the trip, inclusive.")
    budget: Optional[float] = Field(
        None,
        description=(
            "What the trip was meant to cost, as a positive amount. Optional: "
            "a trip with no budget is still worth totalling."
        ),
    )
    notes: Optional[str] = Field(None, description="Anything the dates do not say.")
    split_ways: int = Field(
        1,
        description=(
            "How many people the whole journey was shared between. 1 means it "
            "was not shared. Kept here rather than written onto every "
            "assignment because it is a fact about the trip, not about any "
            "one payment: rewriting 52 shares to say 'we split everything' "
            "would collide with the shares set by hand and could not be "
            "undone. It multiplies with an assignment's own share, which "
            "answers the different question of whether one payment covered "
            "somebody who is not on this trip's books."
        ),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="When the trip was created."
    )

    class Settings:
        name = "trips"
        indexes = [
            # The listing is newest first, and every trip page reads its own
            # window out of the history.
            IndexModel([("start_date", ASCENDING)], name="start_date")
        ]


class TripAssignment(Document):
    """
    One hand-made decision about whether a transaction belongs to a trip.

    Kept out of :class:`Transaction` on purpose. Transactions are rewritten by
    re-import, re-classification and the repair scripts; a decision the user
    made must not be riding along in that blast radius, for the same reason
    :class:`RecurringOverride` is its own collection.

    ``TRIP_EXCLUDED`` matters as much as ``TRIP_INCLUDED``. The suggester
    proposes from a window, so it will keep proposing the same weekly grocery
    shop that happened to fall inside the trip dates until it is told once
    that the answer is no.

    A transaction belongs to at most one trip. Two trips counting the same
    flight would each be individually plausible and jointly a lie, so the
    service moves an assignment rather than letting a second one be written.
    """

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose trip this decision is about."
    )
    trip_id: str = Field(..., description="Id of the trip this decision is about.")
    transaction_id: str = Field(..., description="Id of the transaction decided on.")
    decision: str = Field(..., description=f"One of {sorted(TRIP_DECISIONS)}.")
    share: float = Field(
        1.0,
        description=(
            "The fraction of the amount this trip owns, between 0 and 1. One "
            "booking often covers people who are not on this trip's books — "
            "two flights on one card, a house split four ways — and the whole "
            "amount would overstate the trip while deleting it understates it."
        ),
    )
    decided_at: datetime = Field(
        default_factory=datetime.utcnow, description="When this was last written."
    )

    class Settings:
        name = "trip_assignments"
        indexes = [
            # One decision per transaction per trip. The service also enforces
            # one *inclusion* across all trips, which no index can express.
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("trip_id", ASCENDING),
                    ("transaction_id", ASCENDING),
                ],
                unique=True,
                name="user_trip_transaction",
            ),
            IndexModel(
                [("user_id", ASCENDING), ("transaction_id", ASCENDING)],
                name="user_transaction",
            ),
        ]


class DismissedTripProposal(Document):
    """
    A stretch of history the detector proposed as a trip and the user refused.

    Detection is recomputed from scratch on every page load — a new import can
    turn two clusters into one, so nothing about a proposal is worth storing.
    A refusal is the exception: without it the same wrongly-flagged week of
    online shopping is proposed again after every upload, forever.

    Keyed on the first foreign charge in the cluster rather than on its dates,
    which move as more of the history arrives. This is the same reasoning as
    :class:`RecurringOverride`, which keys on what makes a run distinct rather
    than on any identity the run itself carries.
    """

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose proposal this is."
    )
    anchor_id: str = Field(
        ...,
        description=(
            "Id of the first foreign charge in the refused cluster, which is "
            "what identifies the same proposal on a later run."
        ),
    )
    start_date: datetime = Field(
        ..., description="The window that was refused, for listing it back."
    )
    end_date: datetime = Field(..., description="See ``start_date``.")
    dismissed_at: datetime = Field(
        default_factory=datetime.utcnow, description="When it was refused."
    )

    class Settings:
        name = "dismissed_trip_proposals"
        indexes = [
            IndexModel(
                [("user_id", ASCENDING), ("anchor_id", ASCENDING)],
                unique=True,
                name="user_anchor",
            )
        ]


class PatternDismissal(Document):
    """
    A finding about the spending that the user has looked at and put away.

    Patterns are recomputed from scratch on every page load, for the same
    reason schedules and trip proposals are: a new statement can end a drift,
    start a season and turn an unusual month into an ordinary one, so nothing
    about a finding is worth storing. The refusal is the exception. Without
    it, "cafés are up thirty euros" — noticed, understood, and deliberately
    being paid for — is offered again after every upload, forever.

    Keyed on what the finding *is* rather than on any handle the page carries:
    the kind of question, the thing it is about, and the month it concerns.
    Those three are facts about the history, so they identify the same finding
    on a later run, while a position in a ranked list or a hash of a payload
    would quietly stop matching the moment another finding outranked it. This
    is the same reasoning as :class:`RecurringOverride` and
    :class:`DismissedTripProposal`.

    A dismissal is deliberately *not* permanent for every future version of
    the same subject. Cafés drifting up again next year is a new finding with
    a new month, and it should be offered again — what was dismissed is this
    move, not the category.
    """

    user_id: Indexed(PydanticObjectId) = Field(
        ..., description="Whose finding this is."
    )
    kind: str = Field(
        ...,
        description=(
            "Which question produced the finding, e.g. 'drift' or 'outlier'. "
            "See the KIND_* constants in app.patterns."
        ),
    )
    subject: str = Field(
        "",
        description=(
            "What the finding is about: a category name, a merchant key, or "
            "empty for findings about the ledger as a whole."
        ),
    )
    month: str = Field(
        "",
        description=(
            "The YYYY-MM the finding concerns, or empty where it concerns no "
            "single month, as a season does not."
        ),
    )
    dismissed_at: datetime = Field(
        default_factory=datetime.utcnow, description="When it was put away."
    )

    class Settings:
        name = "pattern_dismissals"
        indexes = [
            IndexModel(
                [
                    ("user_id", ASCENDING),
                    ("kind", ASCENDING),
                    ("subject", ASCENDING),
                    ("month", ASCENDING),
                ],
                unique=True,
                name="user_kind_subject_month",
            )
        ]


class User(Document):
    """
    A person who can sign in.

    Every other document in this file belongs to one, via its ``user_id``.
    An account is made either by hand with ``scripts/create_user.py`` or by
    signing up on the login page itself.
    """

    username: Indexed(str, unique=True) = Field(..., description="Login name.")
    password_hash: str = Field(
        ...,
        description=(
            "salt:digest hex pair from app.auth.hash_password. Never the "
            "plaintext password, and never compared with anything but "
            "app.auth.verify_password."
        ),
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="When the account was made."
    )

    class Settings:
        name = "users"
