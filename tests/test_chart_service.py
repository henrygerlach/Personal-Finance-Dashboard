"""
Tests for the data behind the dashboard's charts and its drill-down.

Two rules run through all of it. Transfers move money without earning or
spending it, so they must not reach a total, an axis or a detail panel — and
the drill-down has to apply exactly the same exclusion as the chart it was
opened from, or the popup contradicts the bar that opened it. The other is
that a month with no activity is still a month: the axis is built from the
range of the history rather than from the months that happen to hold rows,
because a gap silently closed up misdates every bar after it.

The balance service is here too. It is the one part of the app that talks to
somebody else's server, and what matters is that it degrades — a missing
credential or a refused login has to leave the dashboard rendering.
"""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import services as services_module
from app.services import ChartDataService, ComdirectBalanceService
from tests.fixtures.odm import document


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch):
    transaction_model = document(
        "Transaction", "date", "minor_category", "major_category"
    )
    snapshot_model = document("ComdirectBalanceSnapshot", "loaded_at")
    monkeypatch.setattr(services_module, "Transaction", transaction_model)
    monkeypatch.setattr(services_module, "ComdirectBalanceSnapshot", snapshot_model)
    return SimpleNamespace(Transaction=transaction_model, Snapshot=snapshot_model)


def tx(
    db,
    date="2026-07-03",
    amount=-40.0,
    major="Food & Drink",
    minor="Groceries",
    flow="expense",
    **extra,
):
    defaults = dict(
        date=datetime.strptime(date, "%Y-%m-%d"),
        amount=amount,
        booking_text="EDEKA",
        process=None,
        major_category=major,
        minor_category=minor,
        flow_type=flow,
        is_recurring=False,
        paypal_counterparty=None,
    )
    defaults.update(extra)
    row = db.Transaction(**defaults)
    db.Transaction.rows.append(row)
    return row


def transfer(db, date="2026-07-03", amount=-500.0):
    return tx(
        db,
        date=date,
        amount=amount,
        major="Transfers & Savings",
        minor="Investments",
        flow="transfer",
    )


# --- Transfers stay out of everything -----------------------------------------


def test_a_transfer_is_not_in_the_chart(db):
    """
    Moving money between your own accounts is not spending. Counting it makes
    every month with a savings transfer look like a disaster.
    """
    tx(db, amount=-40.0)
    transfer(db)

    data = run(ChartDataService.get_chart_data(year=2026))

    assert "Investments" not in data["categories"]
    assert sum(data["data"]["Groceries"]) == -40.0


def test_a_transfer_is_not_in_the_cashflow(db):
    """Net cashflow is what was earned less what was spent, and this is neither."""
    tx(db, amount=-40.0)
    transfer(db)

    assert run(ChartDataService.get_cashflow_data(year=2026))["total"] == -40.0


def test_a_transfer_is_not_in_the_drill_down(db):
    """
    The panel has to add up to the bar it was opened from. Showing a
    five-hundred euro transfer under a forty euro bar is the clearest possible
    way to say the two disagree.
    """
    tx(db, amount=-40.0)
    transfer(db)

    detail = run(ChartDataService.get_transaction_detail(month="2026-07"))

    assert detail["count"] == 1
    assert detail["net"] == -40.0


# --- The month axis -----------------------------------------------------------


def test_a_month_with_no_transactions_still_appears(db):
    """
    A gap silently closed up puts every later bar under the wrong label, and
    nothing on the page says so.
    """
    tx(db, date="2026-01-10")
    tx(db, date="2026-03-10")

    data = run(ChartDataService.get_chart_data())

    assert data["months"] == ["2026-01", "2026-02", "2026-03"]


def test_asking_for_one_year_draws_all_twelve_months(db):
    """
    A year is a fixed shape. Drawing January to March because that is all
    there is makes a partial year look like a complete one.
    """
    tx(db, date="2026-02-10")

    data = run(ChartDataService.get_chart_data(year=2026))

    assert len(data["months"]) == 12
    assert data["months"][0] == "2026-01"


def test_the_available_years_come_from_the_whole_history(db):
    """
    The year picker has to offer a year even while a different one is
    selected, or choosing one is a one-way trip.
    """
    tx(db, date="2024-05-01")
    tx(db, date="2026-02-10")

    data = run(ChartDataService.get_chart_data(year=2026))

    assert data["available_years"] == [2024, 2026]


def test_an_empty_ledger_produces_an_empty_chart_rather_than_an_error(db):
    """A fresh install renders the dashboard before anything is uploaded."""
    data = run(ChartDataService.get_chart_data())

    assert data == {"months": [], "categories": [], "data": {}, "available_years": []}


def test_the_cashflow_of_an_empty_ledger_is_zero(db):
    assert run(ChartDataService.get_cashflow_data())["total"] == 0


# --- Grouping -----------------------------------------------------------------


def test_the_chart_can_be_drawn_at_either_granularity(db):
    """
    The spectrum is drawn by major category and the breakdown by minor. Both
    read this, so the field has to follow what was asked for.
    """
    tx(db, major="Food & Drink", minor="Groceries")

    minor = run(ChartDataService.get_chart_data())
    major = run(ChartDataService.get_chart_data(category_type="major"))

    assert minor["categories"] == ["Groceries"]
    assert major["categories"] == ["Food & Drink"]


def test_a_transaction_with_no_category_is_grouped_as_other(db):
    """
    A transaction that predates the migration has no category. Dropping it
    would make the chart disagree with the ledger's own total.
    """
    tx(db, minor=None)

    assert "Other" in run(ChartDataService.get_chart_data())["categories"]


def test_asking_for_some_categories_narrows_to_them(db):
    tx(db, minor="Groceries", amount=-40.0)
    tx(db, minor="Rent", amount=-900.0, major="Housing")

    data = run(ChartDataService.get_chart_data(categories=["Rent"]))

    assert data["categories"] == ["Rent"]
    assert sum(data["data"]["Rent"]) == -900.0


def test_a_year_filter_keeps_only_that_year(db):
    tx(db, date="2025-07-03", amount=-10.0)
    tx(db, date="2026-07-03", amount=-40.0)

    data = run(ChartDataService.get_chart_data(year=2026))

    assert sum(data["data"]["Groceries"]) == -40.0


# --- The month a drill-down covers --------------------------------------------


def test_a_month_is_the_month_and_not_a_day_more(db):
    """
    The range is half-open. An inclusive end pulls the first of the next month
    into the bar before it, which is where a rent payment usually sits.
    """
    tx(db, date="2026-07-31", amount=-10.0)
    tx(db, date="2026-08-01", amount=-900.0)

    detail = run(ChartDataService.get_transaction_detail(month="2026-07"))

    assert detail["count"] == 1
    assert detail["net"] == -10.0


def test_december_rolls_into_the_next_year(db):
    """
    Stepping the month index would ask for month 13, which is not a date. This
    is the one month of the year that exercises it.
    """
    tx(db, date="2026-12-15", amount=-10.0)
    tx(db, date="2027-01-02", amount=-900.0)

    detail = run(ChartDataService.get_transaction_detail(month="2026-12"))

    assert detail["count"] == 1


def test_a_month_that_is_not_a_month_is_refused(db):
    """The route validates the shape, and this is the check behind it."""
    with pytest.raises(ValueError, match="YYYY-MM"):
        run(ChartDataService.get_transaction_detail(month="July"))


def test_a_month_wins_over_a_year_because_it_is_more_specific(db):
    """
    Every plot sends both — the month it was clicked on and the year the page
    is set to — so one of them has to lose, and it is the vaguer one.
    """
    tx(db, date="2026-07-03", amount=-40.0)
    tx(db, date="2026-09-03", amount=-90.0)

    detail = run(ChartDataService.get_transaction_detail(month="2026-07", year=2026))

    assert detail["count"] == 1


def test_neither_a_month_nor_a_year_covers_all_of_history(db):
    """The dashboard's "All years" is a real scope, not a missing one."""
    tx(db, date="2024-07-03", amount=-40.0)
    tx(db, date="2026-09-03", amount=-90.0)

    assert run(ChartDataService.get_transaction_detail())["count"] == 2


# --- The two sides of a drill-down --------------------------------------------


def test_the_two_directions_come_back_separately(db):
    """
    What was clicked is a net sum, and a refund filed under an expense
    category belongs to it just as much as the spending. Merged into one
    ranking, a single large refund crowds out the payments it offsets.
    """
    tx(db, amount=-40.0)
    tx(db, amount=25.0, minor="Groceries")

    detail = run(ChartDataService.get_transaction_detail(month="2026-07"))

    assert detail["outgoing"]["count"] == 1
    assert detail["incoming"]["count"] == 1
    assert detail["net"] == -15.0


def test_each_side_is_ranked_by_size(db):
    """The panel is a top-N, so the N have to be the largest ones."""
    tx(db, amount=-10.0, booking_text="SMALL")
    tx(db, amount=-90.0, booking_text="BIG")

    outgoing = run(ChartDataService.get_transaction_detail(month="2026-07"))["outgoing"]

    assert [row["amount"] for row in outgoing["transactions"]] == [-90.0, -10.0]


def test_a_side_says_how_much_of_itself_it_is_not_showing(db):
    """
    Otherwise the list implies it is the whole story, and a bar made of forty
    small payments looks like it is made of eight.
    """
    for index in range(5):
        tx(db, amount=-10.0 * (index + 1))

    outgoing = run(ChartDataService.get_transaction_detail(month="2026-07", limit=2))[
        "outgoing"
    ]

    assert outgoing["count"] == 5
    assert outgoing["hidden_count"] == 3
    assert outgoing["total"] == -150.0
    assert outgoing["rest"] == -60.0


def test_a_side_with_nothing_on_it_is_still_reported(db):
    """The panel renders both sides unconditionally."""
    tx(db, amount=-40.0)

    incoming = run(ChartDataService.get_transaction_detail(month="2026-07"))["incoming"]

    assert incoming == {
        "transactions": [],
        "count": 0,
        "hidden_count": 0,
        "total": 0,
        "rest": 0,
    }


def test_a_drill_down_can_name_several_categories_at_once(db):
    """
    A breakdown collapses its long tail into one "N smaller categories" wedge,
    and that wedge opens like any other slice.
    """
    tx(db, minor="Groceries", amount=-40.0)
    tx(db, minor="Cafés & Snacks", amount=-8.0)
    tx(db, minor="Rent", amount=-900.0, major="Housing")

    detail = run(
        ChartDataService.get_transaction_detail(
            month="2026-07", categories=["Groceries", "Cafés & Snacks"]
        )
    )

    assert detail["count"] == 2
    assert detail["net"] == -48.0


def test_the_recovered_paypal_payee_is_what_the_panel_names(db):
    """
    For a truncated line Comdirect's own booking text says only "PayPal", so
    the panel would otherwise list a column of identical rows.
    """
    tx(
        db,
        booking_text="PP.5189.PP . , Ihr Ei nkauf bei",
        paypal_counterparty="Spotify AB",
    )

    row = run(ChartDataService.get_transaction_detail(month="2026-07"))["outgoing"][
        "transactions"
    ][0]

    assert row["merchant"] == "Spotify AB"
    assert row["booking_text"].startswith("PP.5189.PP")


def test_a_line_with_no_merchant_at_all_is_named_rather_than_blank(db):
    """A blank cell in a list of payments reads as a rendering fault."""
    tx(db, booking_text="")

    row = run(ChartDataService.get_transaction_detail(month="2026-07"))["outgoing"][
        "transactions"
    ][0]

    assert row["merchant"] == "Unnamed payment"


# --- Comdirect balances -------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_snapshot():
    """The snapshot is class state, so it outlives a test that changes it."""
    original = ComdirectBalanceService._snapshot
    yield
    ComdirectBalanceService._snapshot = original


def test_the_balances_are_disabled_and_empty_until_configured(db, monkeypatch):
    """
    The integration is optional. Without it the dashboard has to render, so
    the section is withheld rather than the page failing.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "comdirect_startup_auth_enabled", False)

    run(ComdirectBalanceService.initialize_startup_session())

    assert ComdirectBalanceService._snapshot["status"] == "disabled-no-snapshot"
    assert ComdirectBalanceService.should_render_balance_section() is False


def test_a_stored_snapshot_is_used_when_the_login_is_switched_off(db, monkeypatch):
    """
    Yesterday's balances are worth more than none, as long as the page is told
    they are stale rather than current.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "comdirect_startup_auth_enabled", False)
    db.Snapshot.rows.append(
        db.Snapshot(loaded_at=datetime(2026, 8, 30), balances={"Giro": 1200.0})
    )

    run(ComdirectBalanceService.initialize_startup_session())

    assert ComdirectBalanceService._snapshot["status"] == "stale"
    assert ComdirectBalanceService._snapshot["balances"] == {"Giro": 1200.0}
    assert ComdirectBalanceService.should_render_balance_section() is True


def test_the_newest_stored_snapshot_wins(db, monkeypatch):
    """Balances are a point in time, and the useful point is the last one."""
    from app.config import settings

    monkeypatch.setattr(settings, "comdirect_startup_auth_enabled", False)
    db.Snapshot.rows.append(
        db.Snapshot(loaded_at=datetime(2026, 8, 1), balances={"Giro": 1.0})
    )
    db.Snapshot.rows.append(
        db.Snapshot(loaded_at=datetime(2026, 8, 30), balances={"Giro": 2.0})
    )

    run(ComdirectBalanceService.initialize_startup_session())

    assert ComdirectBalanceService._snapshot["balances"] == {"Giro": 2.0}


def test_a_missing_credential_is_named(db, monkeypatch):
    """
    Four settings are needed and any of them can be absent. Saying which one
    is the difference between a fixable message and a shrug.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "comdirect_startup_auth_enabled", True)
    monkeypatch.setattr(settings, "comdirect_client_id", "id")
    monkeypatch.setattr(settings, "comdirect_client_secret", None)
    monkeypatch.setattr(settings, "comdirect_access_number", "123")
    monkeypatch.setattr(settings, "comdirect_pin", "0000")

    run(ComdirectBalanceService.initialize_startup_session())

    assert ComdirectBalanceService._snapshot["status"] == "error"
    assert "comdirect_client_secret" in ComdirectBalanceService._snapshot["error"]


def test_a_refused_login_falls_back_to_the_stored_snapshot(db, monkeypatch):
    """
    The bank's login can fail for reasons that have nothing to do with this
    app, and it must not take the dashboard down with it.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "comdirect_startup_auth_enabled", True)
    for name in (
        "comdirect_client_id",
        "comdirect_client_secret",
        "comdirect_access_number",
        "comdirect_pin",
    ):
        monkeypatch.setattr(settings, name, "set")
    monkeypatch.setattr(
        ComdirectBalanceService,
        "_authenticate_and_load",
        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("TAN refused"))),
    )
    db.Snapshot.rows.append(
        db.Snapshot(loaded_at=datetime(2026, 8, 30), balances={"Giro": 1200.0})
    )

    run(ComdirectBalanceService.initialize_startup_session())

    assert ComdirectBalanceService._snapshot["status"] == "stale"
    assert "TAN refused" in ComdirectBalanceService._snapshot["error"]


def test_a_refused_login_with_nothing_stored_is_an_error_not_a_crash(db, monkeypatch):
    """There is nothing to show, and the dashboard still has to come up."""
    from app.config import settings

    monkeypatch.setattr(settings, "comdirect_startup_auth_enabled", True)
    for name in (
        "comdirect_client_id",
        "comdirect_client_secret",
        "comdirect_access_number",
        "comdirect_pin",
    ):
        monkeypatch.setattr(settings, name, "set")
    monkeypatch.setattr(
        ComdirectBalanceService,
        "_authenticate_and_load",
        classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no network"))),
    )

    run(ComdirectBalanceService.initialize_startup_session())

    assert ComdirectBalanceService._snapshot["status"] == "error"


def test_a_successful_load_is_stored_for_next_time(db, monkeypatch):
    """
    The stored copy is what the fallback reads, so a run that never writes one
    leaves the next start with nothing to fall back to.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "comdirect_startup_auth_enabled", True)
    for name in (
        "comdirect_client_id",
        "comdirect_client_secret",
        "comdirect_access_number",
        "comdirect_pin",
    ):
        monkeypatch.setattr(settings, name, "set")
    monkeypatch.setattr(
        ComdirectBalanceService,
        "_authenticate_and_load",
        classmethod(
            lambda cls: {
                "status": "ready",
                "loaded_at": datetime(2026, 9, 1).isoformat(),
                "error": None,
                "balances": {"Giro": 1200.0, "Depot": 5000.0},
            }
        ),
    )

    run(ComdirectBalanceService.initialize_startup_session())

    assert ComdirectBalanceService._snapshot["status"] == "ready"
    assert len(db.Snapshot.rows) == 1
    assert db.Snapshot.rows[0].balances == {"Giro": 1200.0, "Depot": 5000.0}


def test_the_snapshot_is_what_the_endpoint_returns(db):
    """The route reads this and nothing else; there is no per-request login."""
    ComdirectBalanceService._snapshot = {"status": "ready", "balances": {"Giro": 1.0}}

    assert ComdirectBalanceService.get_balances_snapshot()["balances"] == {"Giro": 1.0}


# --- Talking to the bank ------------------------------------------------------


def test_the_balances_are_assembled_from_accounts_cards_and_the_depot(monkeypatch):
    """
    Three different shapes from the bank's API, flattened into one dictionary
    the dashboard can draw. The depot has no balance of its own — it is the
    sum of the positions in it, which is why it is added up here rather than
    read off a field.
    """
    import sys
    from types import ModuleType

    module = ModuleType("comdirect_api")

    class Auth:
        def __init__(self, **credentials):
            self.credentials = credentials

        def authenticate(self):
            return True

    class Client:
        def __init__(self, auth):
            self.auth = auth

        def get_account_balances(self):
            return [
                SimpleNamespace(
                    account=SimpleNamespace(
                        accountType=SimpleNamespace(text="Girokonto")
                    ),
                    balance=SimpleNamespace(value=1200.0),
                )
            ]

        def get_card_balances(self):
            return [
                SimpleNamespace(
                    balance=SimpleNamespace(
                        card=SimpleNamespace(cardType=SimpleNamespace(text="Visa")),
                        balance=SimpleNamespace(value=-300.0),
                    )
                )
            ]

        def get_depot_ids(self):
            return [SimpleNamespace(depotId="D1")]

        def get_depot_positions(self, depot_id):
            return [
                SimpleNamespace(currentValue=SimpleNamespace(value="2000.50")),
                SimpleNamespace(currentValue=SimpleNamespace(value="1000.00")),
            ]

    module.ComdirectAuth = Auth
    module.ComdirectClient = Client
    monkeypatch.setitem(sys.modules, "comdirect_api", module)

    snapshot = ComdirectBalanceService._authenticate_and_load()

    assert snapshot["status"] == "ready"
    assert snapshot["balances"] == {
        "Girokonto": 1200.0,
        "Visa": -300.0,
        "Depot": 3000.50,
    }
