"""
Tests for the legacy-to-new category mapping.

The mapping is the only thing standing between 2,600 hand-made categories and
a silent loss of them, so these tests check completeness and validity rather
than spot-checking a few entries.
"""

import pytest

from app.categories import flow_type_for, is_valid_pair
from app.constants import CATEGORY_HIERARCHY as LEGACY_HIERARCHY
from app.legacy_mapping import (
    DIRECT_MAPPING,
    RECLASSIFY,
    invalid_targets,
    legacy_pairs,
    map_pair,
    unmapped_legacy_pairs,
)


def test_every_legacy_pair_is_covered():
    """
    Completeness is the point: an uncovered pair would be discovered only when
    the migration hits it, by which time the run is half done.
    """
    assert unmapped_legacy_pairs() == set()


def test_the_two_tables_do_not_overlap():
    """A pair is either mapped or deferred, never both."""
    assert set(DIRECT_MAPPING) & set(RECLASSIFY) == set()


def test_tables_cover_exactly_the_legacy_hierarchy():
    assert set(DIRECT_MAPPING) | set(RECLASSIFY) == legacy_pairs()


def test_every_target_exists_in_the_new_hierarchy():
    """
    A typo in a target would write a category no filter offers and no colour
    covers, and it would look like real data.
    """
    assert invalid_targets() == set()


@pytest.mark.parametrize("legacy,expected", sorted(DIRECT_MAPPING.items()))
def test_direct_mapping_targets_are_valid_pairs(legacy, expected):
    assert is_valid_pair(*expected)


@pytest.mark.parametrize("legacy,expected", sorted(RECLASSIFY.items()))
def test_reclassify_targets_are_valid_pairs(legacy, expected):
    assert is_valid_pair(*expected)


# --- The decisions that are not obvious from the names ----------------------


def test_received_alimony_stays_income():
    """
    Finances / Alimony is maintenance *received* — about +52,700 EUR over 110
    transactions. Filing it under Family & Education / Alimony & Child Support
    would make it an expense and corrupt every total.
    """
    new_pair, provisional = map_pair("Finances", "Alimony")
    assert new_pair == ("Income", "Other Income")
    assert flow_type_for(new_pair[0]) == "income"
    assert not provisional


def test_driving_school_moves_out_of_transportation():
    """Transportation / Car is a driving school, not a vehicle."""
    assert map_pair("Transportation", "Car")[0] == (
        "Family & Education",
        "Tuition & Courses",
    )


def test_travel_food_and_activities_leave_travel():
    """
    The new hierarchy has no travel-specific food or activity bucket by
    design; grouping a trip is what trip_id is for, not the category.
    """
    assert map_pair("Travel", "Food & Drinks")[0] == (
        "Food & Drink",
        "Restaurants & Bars",
    )
    assert map_pair("Travel", "Activities")[0] == (
        "Leisure & Entertainment",
        "Events & Culture",
    )


def test_investments_and_withdrawals_become_transfers():
    """
    Both were expenses under the old taxonomy and distorted every spending
    chart: about -11,900 EUR of investments counted as money spent.
    """
    for legacy_minor in ("Investments", "Cash Withdrawal"):
        new_pair, _ = map_pair("Finances", legacy_minor)
        assert flow_type_for(new_pair[0]) == "transfer"


def test_legacy_default_maps_to_review():
    """The old "not categorized yet" state is the new one."""
    assert map_pair("Other", "Uncategorized")[0] == ("Uncategorized", "Needs Review")


# --- Deferred buckets --------------------------------------------------------


def test_the_four_deferred_buckets_are_flagged_provisional():
    """
    These four have no honest target. Their provisional category keeps the
    dashboard complete but must be reported as unfinished, or it silently
    becomes the answer.
    """
    assert set(RECLASSIFY) == {
        ("Lifestyle", "Subscriptions"),
        ("Lifestyle", "Shopping"),
        ("Travel", "Transport"),
        ("Other", "Miscellaneous"),
    }
    for legacy in RECLASSIFY:
        assert map_pair(*legacy)[1] is True


def test_directly_mapped_pairs_are_not_provisional():
    for legacy in DIRECT_MAPPING:
        assert map_pair(*legacy)[1] is False


def test_subscriptions_is_deferred_rather_than_mapped():
    """
    The category was dropped on purpose and its contents span two majors —
    streaming services and credit card fees — so no single target is right.
    """
    assert ("Lifestyle", "Subscriptions") in RECLASSIFY
    assert ("Lifestyle", "Subscriptions") not in DIRECT_MAPPING


# --- Failure behaviour -------------------------------------------------------


@pytest.mark.parametrize(
    "legacy",
    [
        ("Groceries", "Food & Drinks"),  # arguments swapped
        ("Food & Drink", "Groceries"),  # already a new-taxonomy pair
        ("Lifestyle", "Invented"),
        ("", ""),
    ],
)
def test_unknown_pairs_raise_instead_of_being_guessed(legacy):
    """
    Guessing here would be indistinguishable from a correct mapping once the
    legacy category is gone, so the migration must stop and report.
    """
    with pytest.raises(KeyError):
        map_pair(*legacy)


def test_mapping_is_deterministic():
    """Same input, same output — the migration reruns and must not drift."""
    for legacy in legacy_pairs():
        assert map_pair(*legacy) == map_pair(*legacy)


def test_no_legacy_major_survives_into_the_new_taxonomy_unchanged_by_accident():
    """
    Two majors keep their name (Transportation, Travel). That is intentional,
    and every other legacy major must actually move.
    """
    unchanged = {
        legacy[0] for legacy, new in DIRECT_MAPPING.items() if legacy[0] == new[0]
    }
    assert unchanged == {"Transportation", "Travel"}
    assert set(LEGACY_HIERARCHY) - unchanged
