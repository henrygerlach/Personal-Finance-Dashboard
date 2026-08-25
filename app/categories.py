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
