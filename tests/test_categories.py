"""
Tests for the category taxonomy.

These guard the invariants the rest of the system relies on: that flow types
are derived and not guessed, that an invalid pair can never reach the
database, and that a minor category name identifies its major on its own.
"""

import pytest

from app.categories import (
    CATEGORY_HIERARCHY,
    CODE_BY_PAIR,
    MAJOR_BY_MINOR,
    NEEDS_REVIEW,
    NEEDS_REVIEW_CODE,
    PAIR_BY_CODE,
    all_codes,
    code_for,
    coerce_pair,
    flow_type_for,
    is_transfer,
    is_valid_code,
    is_valid_major,
    is_valid_pair,
    iter_pairs,
    major_categories,
    minor_categories,
    pair_for_code,
)


def test_hierarchy_is_not_empty_anywhere():
    """Every major must offer at least one minor to file a transaction under."""
    assert CATEGORY_HIERARCHY
    for major, minors in CATEGORY_HIERARCHY.items():
        assert minors, f"{major} has no minor categories"


def test_minor_names_are_unique_across_the_hierarchy():
    """
    A minor name must identify its major on its own.

    The chart API aggregates by minor name without carrying the major along,
    so two majors sharing a minor name would silently merge their totals. The
    old hierarchy had seven categories named "Other" and did exactly that.
    """
    all_minors = [minor for _, minor in iter_pairs()]
    assert len(all_minors) == len(set(all_minors))
    assert len(MAJOR_BY_MINOR) == len(all_minors)


def test_no_minor_collides_with_a_major_name():
    """
    A name may not be both a major and a minor.

    The old hierarchy had "Food & Drinks" as a major and as a minor under
    Travel, which made a category name ambiguous depending on the granularity
    the dashboard was showing.
    """
    assert not set(CATEGORY_HIERARCHY) & set(MAJOR_BY_MINOR)


@pytest.mark.parametrize(
    "major,expected",
    [
        ("Income", "income"),
        ("Transfers & Savings", "transfer"),
        ("Food & Drink", "expense"),
        ("Housing", "expense"),
        ("Uncategorized", "expense"),
    ],
)
def test_flow_type_is_derived_from_the_major(major, expected):
    assert flow_type_for(major) == expected


def test_every_major_yields_a_known_flow_type():
    for major in major_categories():
        assert flow_type_for(major) in {"income", "expense", "transfer"}


def test_unknown_major_counts_as_an_expense():
    """
    A stray category must surface, not vanish.

    Treating the unknown as a transfer would drop it out of every total
    silently; as an expense it shows up in the charts where it is visible.
    """
    assert flow_type_for("Something Invented") == "expense"
    assert not is_transfer("Something Invented")


def test_only_transfers_and_savings_is_a_transfer():
    transfer_majors = [m for m in major_categories() if is_transfer(m)]
    assert transfer_majors == ["Transfers & Savings"]


def test_is_valid_pair_accepts_every_pair_in_the_hierarchy():
    for major, minor in iter_pairs():
        assert is_valid_pair(major, minor)
        assert is_valid_major(major)


@pytest.mark.parametrize(
    "major,minor",
    [
        ("Food & Drink", "Rent"),  # minor from another major
        ("Groceries", "Food & Drink"),  # arguments swapped
        ("Food and Drink", "Groceries"),  # near miss on the major
        ("Food & Drink", "Groceries "),  # trailing space
        ("Food & Drink", "groceries"),  # wrong case
        ("", ""),
    ],
)
def test_is_valid_pair_rejects_near_misses(major, minor):
    """
    Matching is exact on purpose.

    A near miss means the model improvised rather than copied, which is
    exactly the case that should land in review instead of being repaired
    into a category nobody chose.
    """
    assert not is_valid_pair(major, minor)


def test_coerce_pair_passes_valid_pairs_through():
    assert coerce_pair("Food & Drink", "Groceries") == ("Food & Drink", "Groceries")


@pytest.mark.parametrize(
    "major,minor",
    [
        ("Food & Drink", "Rent"),
        ("Invented", "Nonsense"),
        (None, None),
        (123, ["Groceries"]),
        ("Food & Drink", None),
    ],
)
def test_coerce_pair_sends_anything_else_to_review(major, minor):
    assert coerce_pair(major, minor) == NEEDS_REVIEW


def test_needs_review_is_itself_a_valid_pair():
    """The fallback must survive its own validation, or coercion would loop."""
    assert is_valid_pair(*NEEDS_REVIEW)
    assert coerce_pair(*NEEDS_REVIEW) == NEEDS_REVIEW


def test_minor_categories_lookup():
    assert "Groceries" in minor_categories("Food & Drink")
    assert minor_categories("Nope") == []


def test_hierarchy_order_is_preserved():
    """
    Charts stack minors in hierarchy order, so the order is part of the
    contract rather than an accident of the literal.
    """
    assert major_categories()[0] == "Income"
    assert major_categories()[-1] == "Uncategorized"
    assert minor_categories("Income")[0] == "Salary & Wages"


# --- Category codes ----------------------------------------------------------


def test_every_pair_has_a_code_and_every_code_a_pair():
    """The two tables are one mapping seen from both sides."""
    assert len(CODE_BY_PAIR) == len(PAIR_BY_CODE) == len(list(iter_pairs()))
    for pair, code in CODE_BY_PAIR.items():
        assert PAIR_BY_CODE[code] == pair


def test_codes_are_derived_from_hierarchy_order():
    """
    Codes are generated, never maintained by hand, so there is one place to
    change a category. 7.1 is the first minor of the seventh major.
    """
    majors = major_categories()
    assert code_for(majors[6], minor_categories(majors[6])[0]) == "7.1"
    assert code_for(majors[0], minor_categories(majors[0])[0]) == "1.1"
    assert pair_for_code("7.1") == ("Leisure & Entertainment", "Streaming & Media")


def test_a_code_names_a_pair_so_an_impossible_combination_cannot_be_expressed():
    """
    This is the reason codes exist. Two separate name fields can each be valid
    while the pair is not — the model returned "Shopping / Furniture &
    Household Goods" on the first live run, where the minor exists but sits
    under Housing. There is no code for that.
    """
    assert code_for("Shopping", "Furniture & Household Goods") is None
    assert code_for("Housing", "Furniture & Household Goods") is not None
    resolved = {pair_for_code(code) for code in all_codes()}
    assert all(is_valid_pair(*pair) for pair in resolved)


@pytest.mark.parametrize(
    "code", ["99.9", "0.1", "7", "7.", "7.99", "", "seven.one", None, 7.1, "1.1.1"]
)
def test_an_unknown_code_resolves_to_review_rather_than_raising(code):
    """
    A code that is not in the hierarchy means the model produced something
    that is not a category. That belongs in review, not in whichever category
    happens to sit nearby, and it must not take the batch down either.
    """
    assert not is_valid_code(code)
    assert pair_for_code(code) == NEEDS_REVIEW


def test_codes_tolerate_surrounding_whitespace():
    assert pair_for_code(" 7.1 ") == ("Leisure & Entertainment", "Streaming & Media")


def test_the_review_bucket_has_its_own_code():
    assert is_valid_code(NEEDS_REVIEW_CODE)
    assert pair_for_code(NEEDS_REVIEW_CODE) == NEEDS_REVIEW


def test_codes_round_trip_for_every_pair():
    for major, minor in iter_pairs():
        assert pair_for_code(code_for(major, minor)) == (major, minor)
