"""
Tests for the service behind the patterns page.

The detection itself is tested in ``test_patterns.py``; what is defended here
is the layer between it and the page. Three things live in that layer and each
of them has been wrong once: which schedules are handed to the detector, how a
finding is turned into something the page can render, and whether a finding
the user put away stays put away across the recomputation that happens on
every load.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.patterns import KIND_DRIFT, Analysis, Pattern
from app.services import PatternService, RecurringService
from tests.fixtures.odm import patch_services


def item(**extra):
    """One schedule as ``RecurringService.overview`` hands it over."""
    defaults = dict(
        merchant="NETFLIX",
        period="monthly",
        occurrences=12,
        amount=-12.99,
        monthly_equivalent=-12.99,
        annual_equivalent=-155.88,
        major="Leisure & Entertainment",
        minor="Streaming & Media",
        flow_type="expense",
        direction="out",
        status="active",
        first_seen="2025-01-04",
        last_seen="2026-07-04",
        next_expected="2026-08-04",
        days_away=4,
        typical_day=4,
        price_change=None,
        override=None,
        detected_status="active",
        charges=[{"id": "charge-2"}, {"id": "charge-1"}],
    )
    defaults.update(extra)
    return defaults


def test_a_schedule_carries_its_charges_oldest_first():
    """
    The recurring page lists charges newest first because that is how a
    history is read; every series here is built forwards in time, and a
    reversed one would date a price rise to the wrong end of the run.
    """
    schedules = PatternService._schedules([item()])

    assert schedules[0].transaction_ids == ("charge-1", "charge-2")
    assert schedules[0].first_seen == datetime(2025, 1, 4)


def test_a_schedule_the_user_dismissed_is_not_handed_to_the_detector(monkeypatch):
    """
    A dismissed run is not a schedule — the user said so on the recurring
    page — so it must not appear in the committed floor or be reported as
    having stopped collecting. The two pages disagreeing about what a
    schedule is would be worse than either being wrong alone.
    """
    handed = {}

    def fake_analyse(transactions, schedules=(), trip_of=None, reference=None):
        handed["schedules"] = list(schedules)
        return Analysis((), (), (), (), (), (), (), 0.0, "")

    patch_services(monkeypatch, analyse=fake_analyse)
    _patch_reads(
        monkeypatch,
        recurring={
            "items": [
                item(merchant="KEPT"),
                item(merchant="DISMISSED", override="dismissed"),
            ],
            "ended": [],
        },
    )

    asyncio.run(PatternService.overview())

    assert [s.merchant for s in handed["schedules"]] == ["KEPT"]


def _patch_reads(monkeypatch, transactions=(), recurring=None, dismissals=()):
    """Stand in for every collection the overview reads."""

    async def find_all():
        return list(transactions)

    async def overview():
        return recurring or {"items": [], "ended": []}

    async def no_trips():
        return {}

    patch_services(
        monkeypatch,
        Transaction=SimpleNamespace(find_all=lambda: SimpleNamespace(to_list=find_all)),
    )
    patch_services(
        monkeypatch,
        PatternDismissal=SimpleNamespace(
            find_all=lambda: SimpleNamespace(to_list=lambda: _resolve(dismissals))
        ),
    )
    monkeypatch.setattr(RecurringService, "overview", overview)
    monkeypatch.setattr(PatternService, "_trip_names", no_trips)


async def _resolve(value):
    return list(value)


def test_a_finding_carries_the_transactions_that_prove_it():
    """
    A card makes a claim about the money, and evidence that arrives in a
    second request is evidence that can be read after the claim it was
    supposed to support.
    """
    transaction = SimpleNamespace(
        id="t1",
        date=datetime(2026, 7, 12),
        amount=-42.0,
        booking_text=" EDEKA ",
        process=None,
        major_category="Food & Drink",
        minor_category="Groceries",
        is_recurring=False,
        paypal_counterparty=None,
    )
    pattern = Pattern(
        kind=KIND_DRIFT,
        subject="Groceries",
        month="2026-07",
        label="Groceries",
        headline=42.0,
        transaction_ids=("t1", "missing"),
    )

    rendered = PatternService._render(pattern, {"t1": transaction}, frozenset())

    assert rendered["id"] == "drift|Groceries|2026-07"
    assert [row["id"] for row in rendered["rows"]] == ["t1"]


def test_a_finding_the_last_statement_produced_is_marked_new():
    """
    The mark is carried on the finding rather than worked out on the page:
    whether the newest statement produced it is a question about two runs of
    the analysis, and the page has only ever seen one of them.
    """
    pattern = Pattern(
        kind=KIND_DRIFT,
        subject="Groceries",
        month="2026-07",
        label="Groceries",
        headline=42.0,
    )

    assert PatternService._render(pattern, {}, frozenset({pattern.key}))["new"]
    assert not PatternService._render(pattern, {}, frozenset())["new"]


def test_a_finding_that_was_put_away_is_kept_out_of_the_live_list(monkeypatch):
    """
    Findings are recomputed from scratch on every load, so the dismissal has
    to match on the finding's own facts. Anything keyed on a position in a
    ranked list would stop matching the moment another finding outranked it.
    """
    pattern = Pattern(
        kind=KIND_DRIFT,
        subject="Groceries",
        month="2026-07",
        label="Groceries",
        headline=42.0,
    )

    def fake_analyse(transactions, schedules=(), trip_of=None, reference=None):
        return Analysis(
            months=("2026-07",),
            spend=(1000.0,),
            income=(2000.0,),
            committed=(400.0,),
            fixed=(400.0,),
            seasonal=(),
            findings=(pattern,),
            floor=20.0,
            reference="2026-07",
        )

    patch_services(monkeypatch, analyse=fake_analyse)
    _patch_reads(
        monkeypatch,
        dismissals=[
            SimpleNamespace(kind="drift", subject="Groceries", month="2026-07")
        ],
    )

    overview = asyncio.run(PatternService.overview())

    assert overview["count"] == 0
    assert [f["id"] for f in overview["dismissed"]] == ["drift|Groceries|2026-07"]


def test_a_dismissal_of_a_kind_that_does_not_exist_is_refused():
    """It could never match a finding, so it would sit in the collection
    forever doing nothing."""
    with pytest.raises(ValueError):
        asyncio.run(PatternService.dismiss("not-a-kind", "", ""))


def test_the_findings_the_last_statement_produced_are_listed_first(monkeypatch):
    """
    The page is read for what has just happened, so a new finding outranks a
    standing one however large the standing one is — but only against the
    standing ones: within each half the euro order the detector produced has
    to survive, or the largest finding stops being the first one seen.
    """
    standing = Pattern(
        kind=KIND_DRIFT,
        subject="Rent",
        month="2026-07",
        label="Rent",
        headline=900.0,
    )
    fresh_large = Pattern(
        kind=KIND_DRIFT,
        subject="Groceries",
        month="2026-07",
        label="Groceries",
        headline=120.0,
    )
    fresh_small = Pattern(
        kind=KIND_DRIFT,
        subject="Coffee",
        month="2026-07",
        label="Coffee",
        headline=30.0,
    )

    def fake_analyse(transactions, schedules=(), trip_of=None, reference=None):
        return Analysis(
            months=("2026-07",),
            spend=(1000.0,),
            income=(2000.0,),
            committed=(400.0,),
            fixed=(400.0,),
            seasonal=(),
            findings=(standing, fresh_large, fresh_small),
            floor=20.0,
            reference="2026-07",
        )

    patch_services(monkeypatch, analyse=fake_analyse)
    monkeypatch.setattr(
        "app.services.patterns.emerged",
        lambda current, previous: frozenset({fresh_large.key, fresh_small.key}),
    )
    _patch_reads(monkeypatch)

    overview = asyncio.run(PatternService.overview())

    assert [f["label"] for f in overview["findings"]] == ["Groceries", "Coffee", "Rent"]
