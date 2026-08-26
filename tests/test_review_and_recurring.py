"""
Tests for the review queue, the correction path and the recurring overview.

The correction is the piece that has to be right: it settles one transaction
and teaches the merchant cache, so a bad correction spreads.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import services as services_module
from app.services import RecurringService, ReviewService
from app.utils import should_exclude_transaction


def tx(tid="1", major="Uncategorized", minor="Needs Review", **extra):
    defaults = dict(
        id=tid,
        date=datetime(2026, 8, 26),
        amount=-10.0,
        booking_text=" NYA HERING SANIKONZEPT ",
        process=None,
        major_category=major,
        minor_category=minor,
        flow_type=None,
        confidence=0.4,
        reason="unsure",
        classification_source="llm",
        is_recurring=False,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


# --- The exclusion rule ------------------------------------------------------


def test_transfers_are_excluded_by_flow_type():
    """
    The old rule matched two minor-category names and let real money through:
    transfers to other people counted as income, and securities purchases and
    cash withdrawals counted as spending.
    """
    assert should_exclude_transaction(
        tx(major="Transfers & Savings", minor="Investments", flow_type="transfer")
    )
    assert not should_exclude_transaction(
        tx(major="Food & Drink", minor="Groceries", flow_type="expense")
    )
    assert not should_exclude_transaction(
        tx(major="Income", minor="Salary & Wages", flow_type="income")
    )


def test_a_transaction_from_before_the_migration_falls_back_to_the_old_rule():
    """The dashboard has to keep working on a half-migrated database."""
    assert should_exclude_transaction(
        tx(major="Transfers", minor="Internal", flow_type=None)
    )
    assert not should_exclude_transaction(
        tx(major="Food & Drinks", minor="Groceries", flow_type=None)
    )


def test_the_bare_name_form_still_works():
    """Some callers only have the minor category to hand."""
    assert should_exclude_transaction("Internal")
    assert not should_exclude_transaction("Groceries")


# --- Corrections -------------------------------------------------------------


def _patch_correct(monkeypatch, transaction, remembered):
    async def get(_id):
        return transaction

    async def save():
        return None

    async def remember(name, major, minor, source, **kwargs):
        remembered.append((name, major, minor, source))

    transaction.save = save
    monkeypatch.setattr(services_module, "Transaction", SimpleNamespace(get=get))
    monkeypatch.setattr(services_module, "PydanticObjectId", lambda value: value)
    import app.classification.service as classification_service

    monkeypatch.setattr(classification_service, "remember_merchant", remember)


def test_a_correction_files_the_transaction_and_teaches_the_merchant(
    monkeypatch,
):
    """
    This is the point of the queue: one decision settles this transaction and
    every future one from the same merchant, without another API call.
    """
    transaction = tx()
    remembered = []
    _patch_correct(monkeypatch, transaction, remembered)

    import asyncio

    updated, cached = asyncio.run(
        ReviewService.correct("1", "Housing", "Maintenance & Services")
    )

    assert (updated.major_category, updated.minor_category) == (
        "Housing",
        "Maintenance & Services",
    )
    assert updated.flow_type == "expense"
    assert updated.confidence == 1.0
    assert updated.classification_source == "user"
    assert cached is True
    assert remembered == [
        ("NYA HERING SANIKONZEPT", "Housing", "Maintenance & Services", "user")
    ]


def test_an_invalid_pair_is_refused_rather_than_coerced(monkeypatch):
    """
    Coercing it would file the transaction as unreviewed *and* teach the
    merchant cache that answer as if a person had chosen it, so one malformed
    form post would poison every future transaction from that merchant. This
    happened — a form value containing an ampersand arrived split in two.
    """
    transaction = tx()
    remembered = []
    _patch_correct(monkeypatch, transaction, remembered)

    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(ReviewService.correct("1", "Shopping", "Rent"))
    assert remembered == [], "nothing may be cached from a refused correction"


def test_a_correction_on_a_merchantless_line_reports_that_nothing_was_cached(
    monkeypatch,
):
    """A truncated PayPal line has no merchant to key the lesson on."""
    transaction = tx(
        booking_text=(
            "Auftraggeber: PayPal (Europe) S.a r.l. et Cie, S.C.A. "
            "Buchungstext: 102417739 PP.5189.PP . , Ihr Ei nkauf bei Ref. J8/1"
        )
    )
    remembered = []
    _patch_correct(monkeypatch, transaction, remembered)

    import asyncio

    _, cached = asyncio.run(ReviewService.correct("1", "Housing", "Rent"))
    assert cached is False
    assert remembered == []


# --- The recurring overview --------------------------------------------------


def test_transfers_are_kept_out_of_the_recurring_totals(monkeypatch):
    """
    A monthly savings plan is not an expense and a standing order between own
    accounts is not income — the same rule the charts follow. They are still
    listed, because a 500-a-month transfer is worth seeing.
    """
    start = datetime(2025, 1, 10)

    def charge(tid, i, amount, merchant, major, minor, flow):
        return tx(
            tid=tid,
            major=major,
            minor=minor,
            flow_type=flow,
            amount=amount,
            booking_text=f" {merchant} ",
            date=start + timedelta(days=round(i * 30.4)),
        )

    transactions = []
    for i in range(4):
        transactions.append(
            charge(
                f"s{i}",
                i,
                -50.0,
                "SPOTIFY",
                "Leisure & Entertainment",
                "Streaming & Media",
                "expense",
            )
        )
        transactions.append(
            charge(
                f"i{i}",
                i,
                -500.0,
                "BROKER",
                "Transfers & Savings",
                "Investments",
                "transfer",
            )
        )

    class Query:
        async def to_list(self):
            return transactions

    monkeypatch.setattr(
        services_module, "Transaction", SimpleNamespace(find_all=lambda: Query())
    )

    import asyncio

    overview = asyncio.run(RecurringService.overview())

    assert overview["count"] == 2, "both schedules are listed"
    assert overview["monthly_expense"] == pytest.approx(-50.0)
    assert overview["monthly_transfer"] == pytest.approx(-500.0)
    assert overview["transfer_count"] == 1


def test_yearly_charges_are_shown_at_their_monthly_equivalent(monkeypatch):
    """
    Otherwise the total is meaningless: a 90 EUR annual premium would sit next
    to a 4.99 subscription as if it cost twenty times as much every month.
    """
    start = datetime(2023, 3, 1)
    transactions = [
        tx(
            tid=f"y{i}",
            major="Financial",
            minor="Insurance",
            flow_type="expense",
            amount=-90.0,
            booking_text=" INSURER ",
            date=start + timedelta(days=365 * i),
        )
        for i in range(3)
    ]

    class Query:
        async def to_list(self):
            return transactions

    monkeypatch.setattr(
        services_module, "Transaction", SimpleNamespace(find_all=lambda: Query())
    )

    import asyncio

    overview = asyncio.run(RecurringService.overview())
    assert overview["items"][0]["period"] == "yearly"
    assert overview["items"][0]["monthly_equivalent"] == pytest.approx(-7.5)
    assert overview["monthly_expense"] == pytest.approx(-7.5)
