"""
The explicit old-taxonomy to new-taxonomy mapping.

Every pair of the legacy hierarchy in ``app.constants`` appears here exactly
once, either in ``DIRECT_MAPPING`` or in ``RECLASSIFY``. Nothing is inferred
from name similarity: the two hierarchies disagree in ways no heuristic would
get right, and a wrong guess would be indistinguishable from a correct one
once the old category is gone.

The decisions worth recording, because the target is not obvious from the
names:

``Finances / Alimony`` is *received* maintenance — 110 transactions summing to
about +52,700 EUR. The new hierarchy only offers ``Family & Education /
Alimony & Child Support``, which sits on the expense side; filing income there
would flip the flow type and corrupt every total. It maps to ``Income``.

``Transportation / Car`` is a driving school, not a vehicle. It maps to
``Family & Education / Tuition & Courses``.

``Travel / Food & Drinks`` and ``Travel / Activities`` move out of Travel into
their subject categories. The new hierarchy has no travel-specific food or
activity bucket by design; grouping a trip back together is what the optional
``trip_id`` is for, not the category.

``Transfers / External`` is a mixed bucket — gifts from family, an employer
payment, PayPal refunds, and person-to-person transfers out. It maps to
``Transfers & Savings / Person to Person``, which covers the majority.

Four legacy pairs have no honest target at all and are listed in
``RECLASSIFY`` instead. They get a provisional category so the dashboard stays
intact, are marked ``pending``, and are sent through the classifier once it is
in place.
"""

from app.categories import is_valid_pair
from app.constants import CATEGORY_HIERARCHY as LEGACY_HIERARCHY

LegacyPair = tuple[str, str]

#: Legacy pair -> new pair. Applied as-is by the migration.
DIRECT_MAPPING: dict[LegacyPair, LegacyPair] = {
    # --- Finances: splits across Income and Transfers & Savings -------------
    ("Finances", "Salary"): ("Income", "Salary & Wages"),
    # Received maintenance, not paid. See the module docstring.
    ("Finances", "Alimony"): ("Income", "Other Income"),
    ("Finances", "Investments"): ("Transfers & Savings", "Investments"),
    ("Finances", "Cash Withdrawal"): ("Transfers & Savings", "Cash Withdrawal"),
    # A single incoming transaction of +50 EUR.
    ("Finances", "Other"): ("Income", "Other Income"),
    # --- Living -> Housing --------------------------------------------------
    ("Living", "Rent"): ("Housing", "Rent"),
    ("Living", "Utilities"): ("Housing", "Utilities"),
    ("Living", "Household"): ("Housing", "Furniture & Household Goods"),
    ("Living", "Other"): ("Housing", "Other Housing"),
    # --- Food & Drinks -> Food & Drink --------------------------------------
    ("Food & Drinks", "Groceries"): ("Food & Drink", "Groceries"),
    ("Food & Drinks", "Dining Out"): ("Food & Drink", "Restaurants & Bars"),
    ("Food & Drinks", "Delivery"): ("Food & Drink", "Delivery & Takeout"),
    ("Food & Drinks", "Coffee & Snacks"): ("Food & Drink", "Cafés & Snacks"),
    ("Food & Drinks", "Other"): ("Food & Drink", "Other Food & Drink"),
    # --- Lifestyle: splits three ways ---------------------------------------
    ("Lifestyle", "Entertainment"): ("Leisure & Entertainment", "Events & Culture"),
    ("Lifestyle", "Hobbies"): ("Leisure & Entertainment", "Hobbies & Activities"),
    ("Lifestyle", "Personal Care"): (
        "Health & Personal Care",
        "Personal Care & Beauty",
    ),
    ("Lifestyle", "Clothing"): ("Shopping", "Clothing & Shoes"),
    ("Lifestyle", "Other"): ("Leisure & Entertainment", "Other Leisure"),
    # --- Health -> Health & Personal Care -----------------------------------
    ("Health", "Insurance"): ("Health & Personal Care", "Health Insurance"),
    ("Health", "Pharmacy"): ("Health & Personal Care", "Pharmacy & Medication"),
    ("Health", "Medical Care"): ("Health & Personal Care", "Medical Care"),
    ("Health", "Wellness"): ("Health & Personal Care", "Fitness & Sports"),
    ("Health", "Other"): ("Health & Personal Care", "Other Health"),
    # --- Transportation -----------------------------------------------------
    ("Transportation", "Public Transit"): ("Transportation", "Public Transit"),
    # A driving school. See the module docstring.
    ("Transportation", "Car"): ("Family & Education", "Tuition & Courses"),
    ("Transportation", "Sharing"): ("Transportation", "Taxi & Sharing"),
    ("Transportation", "Other"): ("Transportation", "Other Transportation"),
    # --- Travel: food and activities leave Travel ---------------------------
    ("Travel", "Accommodation"): ("Travel", "Accommodation"),
    ("Travel", "Food & Drinks"): ("Food & Drink", "Restaurants & Bars"),
    ("Travel", "Activities"): ("Leisure & Entertainment", "Events & Culture"),
    ("Travel", "Other"): ("Travel", "Other Travel"),
    # --- Transfers -> Transfers & Savings -----------------------------------
    ("Transfers", "Internal"): ("Transfers & Savings", "Internal Transfer"),
    ("Transfers", "External"): ("Transfers & Savings", "Person to Person"),
    # --- Other --------------------------------------------------------------
    ("Other", "Uncategorized"): ("Uncategorized", "Needs Review"),
}

#: Legacy pairs with no honest target, and the provisional category they hold
#: until the classifier reaches them. These are marked ``pending``: the
#: provisional value keeps the dashboard complete, it is not an answer.
RECLASSIFY: dict[LegacyPair, LegacyPair] = {
    # The old taxonomy dropped "Subscriptions" by design. The bucket mixes
    # streaming services with credit card fees, so it cannot map anywhere.
    ("Lifestyle", "Subscriptions"): ("Leisure & Entertainment", "Streaming & Media"),
    # About -9,700 EUR under one label — too coarse to file blind, and it
    # visibly contains electronics.
    ("Lifestyle", "Shopping"): ("Shopping", "General Merchandise"),
    # Flights, trains and rental cars are separate categories now.
    ("Travel", "Transport"): ("Travel", "Flights & Long-Distance"),
    # Mostly PayPal debits whose merchant Comdirect truncated away.
    ("Other", "Miscellaneous"): ("Shopping", "General Merchandise"),
}


def legacy_pairs() -> set[LegacyPair]:
    """Every pair the legacy hierarchy defines."""
    return {
        (major, minor)
        for major, minors in LEGACY_HIERARCHY.items()
        for minor in minors
    }


def map_pair(major: str, minor: str) -> tuple[LegacyPair, bool]:
    """
    Translate one legacy pair.

    Returns:
        The new ``(major, minor)`` and whether it is provisional — that is,
        whether the transaction still needs to be classified.

    Raises:
        KeyError: If the pair is not in the legacy hierarchy. The migration
            surfaces those rather than filing them somewhere plausible.
    """
    if (major, minor) in DIRECT_MAPPING:
        return DIRECT_MAPPING[(major, minor)], False
    if (major, minor) in RECLASSIFY:
        return RECLASSIFY[(major, minor)], True
    raise KeyError(f"No mapping for legacy category {major!r} / {minor!r}")


def unmapped_legacy_pairs() -> set[LegacyPair]:
    """Legacy pairs that neither table covers. Must always be empty."""
    return legacy_pairs() - set(DIRECT_MAPPING) - set(RECLASSIFY)


def invalid_targets() -> set[LegacyPair]:
    """Mapping targets that are not pairs of the new hierarchy. Must be empty."""
    targets = set(DIRECT_MAPPING.values()) | set(RECLASSIFY.values())
    return {pair for pair in targets if not is_valid_pair(*pair)}
