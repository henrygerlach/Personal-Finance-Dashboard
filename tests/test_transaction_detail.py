"""
Tests for the drill-down every plot on the dashboard opens.

The piece that has to be right is the direction split. What a click names is a
*net* sum, so a refund filed under an expense category belongs to the thing it
shortens. Merging both directions into one "biggest movements" list would let a
single large refund push out the spending it offsets, and showing only the
negative side would make the list disagree with the plot it was opened from.

The second piece is scope: month, year and category have to compose, because
one endpoint answers for a bar of the monthly chart, a slice of a breakdown and
a segment of the spectrum alike.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import services as services_module
from app.services import TOP_DETAIL_TRANSACTIONS, ChartDataService


def tx(tid="1", amount=-10.0, day=14, **extra):
    defaults = dict(
        id=tid,
        date=datetime(2026, 3, day),
        amount=amount,
        booking_text=" EDEKA BOECKER ",
        process=None,
        major_category="Food & Drink",
        minor_category="Groceries",
        flow_type="expense",
        is_recurring=False,
        paypal_counterparty=None,
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


class _DateField:
    """Stands in for the indexed ``date`` field inside a query expression."""

    def __ge__(self, value):
        return ("gte", value)

    def __lt__(self, value):
        return ("lt", value)


def use_transactions(monkeypatch, transactions):
    """Point the service at a fixed set of transactions, honouring its filter."""

    class Query:
        def __init__(self, conditions):
            self.conditions = conditions

        async def to_list(self):
            rows = list(transactions)
            for operator, value in self.conditions:
                if operator == "gte":
                    rows = [row for row in rows if row.date >= value]
                else:
                    rows = [row for row in rows if row.date < value]
            return rows

    monkeypatch.setattr(
        services_module,
        "Transaction",
        SimpleNamespace(
            date=_DateField(),
            find=lambda *conditions: Query(conditions),
            find_all=lambda: Query(()),
        ),
    )


def detail(**kwargs):
    return asyncio.run(ChartDataService.get_transaction_detail(**kwargs))


# --- The direction split -----------------------------------------------------


def test_a_refund_inside_an_expense_band_is_listed_on_its_own_side(monkeypatch):
    """
    The band is one number, but two things made it: 90 EUR of groceries and a
    30 EUR refund. Both have to be visible, or the panel cannot explain why
    the bar is shorter than the spending was.
    """
    use_transactions(
        monkeypatch,
        [
            tx(tid="a", amount=-60.0),
            tx(tid="b", amount=-30.0),
            tx(tid="c", amount=30.0, booking_text=" EDEKA BOECKER GUTSCHRIFT "),
        ],
    )

    result = detail(month="2026-03", category="Groceries")

    assert result["net"] == pytest.approx(-60.0)
    assert result["count"] == 3
    assert result["outgoing"]["count"] == 2
    assert result["outgoing"]["total"] == pytest.approx(-90.0)
    assert result["incoming"]["count"] == 1
    assert result["incoming"]["total"] == pytest.approx(30.0)


def test_each_side_is_ranked_by_size(monkeypatch):
    """Top means largest movement, so the biggest one leads its side."""
    use_transactions(
        monkeypatch,
        [
            tx(tid="small", amount=-5.0),
            tx(tid="big", amount=-500.0),
            tx(tid="mid", amount=-50.0),
        ],
    )

    result = detail(month="2026-03", category="Groceries")
    order = [row["id"] for row in result["outgoing"]["transactions"]]

    assert order == ["big", "mid", "small"]


def test_the_rows_beyond_the_limit_are_counted_not_dropped(monkeypatch):
    """
    A truncated list that says nothing about what it left out would read as
    the whole story, and the amounts would not add up to the plot.
    """
    limit = TOP_DETAIL_TRANSACTIONS
    use_transactions(
        monkeypatch,
        [tx(tid=str(i), amount=-float(i + 1)) for i in range(limit + 3)],
    )

    result = detail(month="2026-03", category="Groceries", limit=limit)
    outgoing = result["outgoing"]

    assert len(outgoing["transactions"]) == limit
    assert outgoing["hidden_count"] == 3
    # The three smallest charges are the ones held back: 1 + 2 + 3.
    assert outgoing["rest"] == pytest.approx(-6.0)


# --- Which transactions belong to the bar ------------------------------------


def test_transfers_stay_out_of_the_drill_down(monkeypatch):
    """
    The charts exclude transfers, so a panel that included them would not add
    up to the bar it was opened from.
    """
    use_transactions(
        monkeypatch,
        [
            tx(tid="spend", amount=-40.0),
            tx(
                tid="moved",
                amount=-1000.0,
                major_category="Transfers & Savings",
                minor_category="Groceries",
                flow_type="transfer",
            ),
        ],
    )

    result = detail(month="2026-03", category="Groceries")

    assert result["count"] == 1
    assert result["net"] == pytest.approx(-40.0)


def test_a_major_granularity_click_filters_on_the_major_category(monkeypatch):
    """The chart can be drawn at either granularity, and the panel must agree."""
    use_transactions(
        monkeypatch,
        [
            tx(tid="groceries", amount=-40.0),
            tx(
                tid="restaurant",
                amount=-25.0,
                minor_category="Restaurants & Cafes",
            ),
            tx(
                tid="fuel",
                amount=-70.0,
                major_category="Transportation",
                minor_category="Fuel",
            ),
        ],
    )

    result = detail(month="2026-03", category="Food & Drink", category_type="major")

    assert result["count"] == 2
    assert result["net"] == pytest.approx(-65.0)


def test_omitting_the_category_covers_every_category(monkeypatch):
    """The endpoint also answers for a month as a whole."""
    use_transactions(
        monkeypatch,
        [
            tx(tid="groceries", amount=-40.0),
            tx(
                tid="fuel",
                amount=-70.0,
                major_category="Transportation",
                minor_category="Fuel",
            ),
        ],
    )

    assert detail(month="2026-03")["count"] == 2


def test_only_the_clicked_month_is_read(monkeypatch):
    """A neighbouring month must not leak into the bar."""
    use_transactions(
        monkeypatch,
        [
            tx(tid="february", amount=-40.0, date=datetime(2026, 2, 28, 23, 59)),
            tx(tid="march", amount=-70.0, date=datetime(2026, 3, 1)),
            tx(tid="april", amount=-90.0, date=datetime(2026, 4, 1)),
        ],
    )

    result = detail(month="2026-03", category="Groceries")

    assert [row["id"] for row in result["outgoing"]["transactions"]] == ["march"]


# --- Month bounds ------------------------------------------------------------


def test_december_rolls_into_the_next_january():
    """The obvious off-by-one: month 12 has no month 13 to stop at."""
    start, end = ChartDataService._month_bounds("2025-12")

    assert start == datetime(2025, 12, 1)
    assert end == datetime(2026, 1, 1)


def test_a_month_that_is_not_a_month_is_refused():
    with pytest.raises(ValueError):
        ChartDataService._month_bounds("2026-13")


# --- How a row is described --------------------------------------------------


def test_a_paypal_row_shows_the_recovered_counterparty(monkeypatch):
    """
    Comdirect's own line for these says only "PayPal", so the counterparty the
    export recovered is the only thing on the row worth reading.
    """
    use_transactions(
        monkeypatch,
        [
            tx(
                tid="pp",
                amount=-19.99,
                booking_text=" PayPal Europe S.a.r.l. et Cie S.C.A ",
                paypal_counterparty="Bandcamp",
            )
        ],
    )

    row = detail(month="2026-03", category="Groceries")["outgoing"]["transactions"][0]

    assert row["merchant"] == "Bandcamp"
    assert row["booking_text"] == " PayPal Europe S.a.r.l. et Cie S.C.A "


# --- Scope ------------------------------------------------------------------


def spread(monkeypatch):
    """One transaction a year, so a scope either catches it or it does not."""
    use_transactions(
        monkeypatch,
        [
            tx(tid="2024", amount=-10.0, date=datetime(2024, 6, 1)),
            tx(tid="2025", amount=-20.0, date=datetime(2025, 6, 1)),
            tx(tid="2026", amount=-40.0, date=datetime(2026, 6, 1)),
        ],
    )


def test_a_year_covers_that_year_alone(monkeypatch):
    """A breakdown slice is a category over the year the filter is set to."""
    spread(monkeypatch)

    result = detail(year=2025, category="Groceries")

    assert result["count"] == 1
    assert result["net"] == pytest.approx(-20.0)


def test_omitting_month_and_year_covers_all_of_history(monkeypatch):
    """Which is what the dashboard's "All years" asks for."""
    spread(monkeypatch)

    result = detail(category="Groceries")

    assert result["count"] == 3
    assert result["net"] == pytest.approx(-70.0)


def test_a_month_outranks_a_year(monkeypatch):
    """The month already names its year, so a stale year must not narrow it."""
    spread(monkeypatch)

    result = detail(month="2026-06", year=2024, category="Groceries")

    assert [row["id"] for row in result["outgoing"]["transactions"]] == ["2026"]


def test_the_scope_comes_back_with_the_answer(monkeypatch):
    """The popup titles itself from what it asked for, so it has to be echoed."""
    spread(monkeypatch)

    result = detail(year=2025, category="Food & Drink", category_type="major")

    assert result["month"] is None
    assert result["year"] == 2025
    assert result["category"] == "Food & Drink"
    assert result["category_type"] == "major"
