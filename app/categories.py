"""
The category taxonomy: the single source of truth for major/minor pairs.

Every part of the system reads its categories from here — the classification
prompt, the migration mapping, the review UI, the chart colours and the
dashboard filters. Nothing else may define a category list.

A transaction carries exactly one (major, minor) pair. Its ``flow_type`` is
*derived* from the major category and never guessed: everything under
``Income`` is income, everything under ``Transfers & Savings`` is a transfer,
and the rest is an expense. Transfers move money without earning or spending
it, so they must stay out of every expense total, expense chart and monthly
comparison.

The legacy hierarchy in ``app/constants.py`` is still in use by the rule
engine and stays untouched until the switch-over is complete.
"""

from typing import Literal

FlowType = Literal["income", "expense", "transfer"]

# Majors that are not expenses. Both are referenced by name elsewhere in this
# module, so they are named constants rather than repeated string literals.
MAJOR_INCOME = "Income"
MAJOR_TRANSFER = "Transfers & Savings"
MAJOR_UNCATEGORIZED = "Uncategorized"

MINOR_NEEDS_REVIEW = "Needs Review"

#: Where anything unclassifiable lands: an invalid pair from the model, a
#: confidence below the threshold, or a batch that failed outright.
NEEDS_REVIEW: tuple[str, str] = (MAJOR_UNCATEGORIZED, MINOR_NEEDS_REVIEW)

CATEGORY_HIERARCHY: dict[str, list[str]] = {
    "Income": [
        "Salary & Wages",
        "Self-Employment",
        "Benefits & Pension",
        "Investment & Rental Income",
        "Refunds & Reimbursements",
        "Other Income",
    ],
    "Housing": [
        "Rent",
        "Mortgage & Property Costs",
        "Utilities",
        "Internet & Phone",
        "Maintenance & Services",
        "Furniture & Household Goods",
        "Other Housing",
    ],
    "Food & Drink": [
        "Groceries",
        "Restaurants & Bars",
        "Cafés & Snacks",
        "Delivery & Takeout",
        "Other Food & Drink",
    ],
    "Transportation": [
        "Public Transit",
        "Fuel & Charging",
        "Car Maintenance & Repairs",
        "Car Insurance & Tax",
        "Parking & Tolls",
        "Taxi & Sharing",
        "Vehicle Purchase & Leasing",
        "Other Transportation",
    ],
    "Health & Personal Care": [
        "Health Insurance",
        "Medical Care",
        "Pharmacy & Medication",
        "Fitness & Sports",
        "Personal Care & Beauty",
        "Other Health",
    ],
    "Shopping": [
        "Clothing & Shoes",
        "Electronics & Tech",
        "Home & Garden",
        "Sports & Outdoor Gear",
        "General Merchandise",
        "Other Shopping",
    ],
    "Leisure & Entertainment": [
        "Streaming & Media",
        "Events & Culture",
        "Hobbies & Activities",
        "Software & Apps",
        "Games",
        "Other Leisure",
    ],
    "Travel": [
        "Flights & Long-Distance",
        "Accommodation",
        "Package Tours & Bookings",
        "Rental Car",
        "Other Travel",
    ],
    "Family & Education": [
        "Childcare & School",
        "Alimony & Child Support",
        "Tuition & Courses",
        "Books & Learning Materials",
        "Other Family & Education",
    ],
    "Pets": [
        "Pet Food & Supplies",
        "Vet & Pet Health",
        "Other Pets",
    ],
    "Financial": [
        "Bank & Card Fees",
        "Interest & Loan Costs",
        "Taxes",
        "Insurance",
        "Other Financial",
    ],
    "Gifts & Donations": [
        "Gifts",
        "Donations & Charity",
    ],
    "Transfers & Savings": [
        "Internal Transfer",
        "Cash Withdrawal",
        "Cash Deposit",
        "Savings Contribution",
        "Investments",
        "Credit Card Payment",
        "Loan Repayment",
        "Person to Person",
    ],
    "Uncategorized": [MINOR_NEEDS_REVIEW],
}

#: Minor -> major. Minor names are unique across the whole hierarchy (see the
#: test suite), so a minor category alone identifies its major. The dashboard
#: API aggregates by minor name, and relies on that.
MAJOR_BY_MINOR: dict[str, str] = {
    minor: major for major, minors in CATEGORY_HIERARCHY.items() for minor in minors
}


def major_categories() -> list[str]:
    """All major categories, in hierarchy order."""
    return list(CATEGORY_HIERARCHY)


def minor_categories(major: str) -> list[str]:
    """The minor categories of one major, in hierarchy order. Empty if unknown."""
    return list(CATEGORY_HIERARCHY.get(major, ()))


def iter_pairs():
    """Yield every valid ``(major, minor)`` pair in hierarchy order."""
    for major, minors in CATEGORY_HIERARCHY.items():
        for minor in minors:
            yield major, minor


def is_valid_major(major: str) -> bool:
    """Whether ``major`` is a known major category."""
    return major in CATEGORY_HIERARCHY


def is_valid_pair(major: str, minor: str) -> bool:
    """
    Whether ``(major, minor)`` is a pair the hierarchy actually contains.

    Matching is exact. The classifier is instructed to return categories
    verbatim, and a near miss ("Restaurants and Bars", "Groceries ") is a
    signal that the model improvised — it should fall through to review
    rather than be silently repaired.
    """
    return minor in CATEGORY_HIERARCHY.get(major, ())


def coerce_pair(major: object, minor: object) -> tuple[str, str]:
    """
    Return ``(major, minor)`` if the hierarchy contains it, else ``NEEDS_REVIEW``.

    This is the server-side gate every classification result passes through,
    so an invalid pair can never reach the database.
    """
    if not (isinstance(major, str) and isinstance(minor, str)):
        return NEEDS_REVIEW
    if is_valid_pair(major, minor):
        return major, minor
    return NEEDS_REVIEW


def flow_type_for(major: str) -> FlowType:
    """
    Derive the flow type from the major category.

    Unknown majors count as an expense, which is the conservative choice: a
    stray category shows up in the spending charts where it is visible, rather
    than silently vanishing the way a mislabelled transfer would.
    """
    if major == MAJOR_INCOME:
        return "income"
    if major == MAJOR_TRANSFER:
        return "transfer"
    return "expense"


def is_transfer(major: str) -> bool:
    """Whether transactions of this major must stay out of income and expense totals."""
    return flow_type_for(major) == "transfer"


# --- Category codes ----------------------------------------------------------
#
# The classifier answers with a code like "7.1" rather than two category names.
# Two reasons, in order of importance:
#
# A code names a *pair*, so an impossible combination cannot be expressed. Two
# separate name fields can each be valid while the pair is not — the model
# returned "Shopping / Furniture & Household Goods" on the very first live
# test, where the minor exists but sits under Housing. A code has no way to
# say that.
#
# Output tokens cost several times input tokens, and the category name is the
# bulk of each answer. That saving is real but small at this volume; it is a
# side benefit, not the reason.
#
# Codes are *derived* from the hierarchy's order, never maintained by hand, so
# there is one place to change a category. The consequence is that reordering
# CATEGORY_HIERARCHY renumbers everything: a code is a wire format, and
# `major`/`minor` stay the stored truth. A stored code is a convenience for
# reading, and must never be resolved back into a category on its own.

#: Pair -> code, numbered from 1 in hierarchy order.
CODE_BY_PAIR: dict[tuple[str, str], str] = {}
#: Code -> pair.
PAIR_BY_CODE: dict[str, tuple[str, str]] = {}

for _major_index, (_major, _minors) in enumerate(CATEGORY_HIERARCHY.items(), start=1):
    for _minor_index, _minor in enumerate(_minors, start=1):
        _code = f"{_major_index}.{_minor_index}"
        CODE_BY_PAIR[(_major, _minor)] = _code
        PAIR_BY_CODE[_code] = (_major, _minor)

#: The code the review bucket carries, so callers need not look it up.
NEEDS_REVIEW_CODE = CODE_BY_PAIR[NEEDS_REVIEW]


def code_for(major: str, minor: str) -> str | None:
    """The code for a pair, or ``None`` if the hierarchy does not contain it."""
    return CODE_BY_PAIR.get((major, minor))


def pair_for_code(code: object) -> tuple[str, str]:
    """
    Resolve a code into its pair, falling back to review.

    A code the hierarchy does not define is not an error: the model produced
    something that is not a category, and the transaction belongs in review
    rather than in whichever category happens to sit nearby.
    """
    if isinstance(code, str) and code.strip() in PAIR_BY_CODE:
        return PAIR_BY_CODE[code.strip()]
    return NEEDS_REVIEW


def is_valid_code(code: object) -> bool:
    """Whether ``code`` names a pair in the hierarchy."""
    return isinstance(code, str) and code.strip() in PAIR_BY_CODE


def all_codes() -> list[str]:
    """Every code, in hierarchy order."""
    return list(PAIR_BY_CODE)
