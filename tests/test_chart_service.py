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
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.services import ChartDataService, ComdirectBalanceService
from tests.fixtures.odm import document, patch_services


def run(coro):
    return asyncio.run(coro)


USER_ID = "u" * 24


@pytest.fixture
def db(monkeypatch):
    transaction_model = document(
        "Transaction", "date", "minor_category", "major_category", "user_id"
    )
    snapshot_model = document("ComdirectBalanceSnapshot", "user_id", "loaded_at")
    credentials_model = document("ComdirectCredentials", "user_id")
    patch_services(
        monkeypatch,
        Transaction=transaction_model,
        ComdirectBalanceSnapshot=snapshot_model,
        ComdirectCredentials=credentials_model,
    )
    return SimpleNamespace(
        Transaction=transaction_model,
        Snapshot=snapshot_model,
        Credentials=credentials_model,
    )


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
        user_id=USER_ID,
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

    data = run(ChartDataService.get_chart_data(USER_ID, year=2026))

    assert "Investments" not in data["categories"]
    assert sum(data["data"]["Groceries"]) == -40.0


def test_a_transfer_is_not_in_the_cashflow(db):
    """Net cashflow is what was earned less what was spent, and this is neither."""
    tx(db, amount=-40.0)
    transfer(db)

    assert run(ChartDataService.get_cashflow_data(USER_ID, year=2026))["total"] == -40.0


def test_a_transfer_is_not_in_the_drill_down(db):
    """
    The panel has to add up to the bar it was opened from. Showing a
    five-hundred euro transfer under a forty euro bar is the clearest possible
    way to say the two disagree.
    """
    tx(db, amount=-40.0)
    transfer(db)

    detail = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-07"))

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

    data = run(ChartDataService.get_chart_data(USER_ID))

    assert data["months"] == ["2026-01", "2026-02", "2026-03"]


def test_asking_for_one_year_draws_all_twelve_months(db):
    """
    A year is a fixed shape. Drawing January to March because that is all
    there is makes a partial year look like a complete one.
    """
    tx(db, date="2026-02-10")

    data = run(ChartDataService.get_chart_data(USER_ID, year=2026))

    assert len(data["months"]) == 12
    assert data["months"][0] == "2026-01"


def test_the_available_years_come_from_the_whole_history(db):
    """
    The year picker has to offer a year even while a different one is
    selected, or choosing one is a one-way trip.
    """
    tx(db, date="2024-05-01")
    tx(db, date="2026-02-10")

    data = run(ChartDataService.get_chart_data(USER_ID, year=2026))

    assert data["available_years"] == [2024, 2026]


def test_an_empty_ledger_produces_an_empty_chart_rather_than_an_error(db):
    """A fresh install renders the dashboard before anything is uploaded."""
    data = run(ChartDataService.get_chart_data(USER_ID))

    assert data == {"months": [], "categories": [], "data": {}, "available_years": []}


def test_the_cashflow_of_an_empty_ledger_is_zero(db):
    assert run(ChartDataService.get_cashflow_data(USER_ID))["total"] == 0


# --- Grouping -----------------------------------------------------------------


def test_the_chart_can_be_drawn_at_either_granularity(db):
    """
    The spectrum is drawn by major category and the breakdown by minor. Both
    read this, so the field has to follow what was asked for.
    """
    tx(db, major="Food & Drink", minor="Groceries")

    minor = run(ChartDataService.get_chart_data(USER_ID))
    major = run(ChartDataService.get_chart_data(USER_ID, category_type="major"))

    assert minor["categories"] == ["Groceries"]
    assert major["categories"] == ["Food & Drink"]


def test_a_transaction_with_no_category_is_grouped_as_other(db):
    """
    A transaction that predates the migration has no category. Dropping it
    would make the chart disagree with the ledger's own total.
    """
    tx(db, minor=None)

    assert "Other" in run(ChartDataService.get_chart_data(USER_ID))["categories"]


def test_asking_for_some_categories_narrows_to_them(db):
    tx(db, minor="Groceries", amount=-40.0)
    tx(db, minor="Rent", amount=-900.0, major="Housing")

    data = run(ChartDataService.get_chart_data(USER_ID, categories=["Rent"]))

    assert data["categories"] == ["Rent"]
    assert sum(data["data"]["Rent"]) == -900.0


def test_a_year_filter_keeps_only_that_year(db):
    tx(db, date="2025-07-03", amount=-10.0)
    tx(db, date="2026-07-03", amount=-40.0)

    data = run(ChartDataService.get_chart_data(USER_ID, year=2026))

    assert sum(data["data"]["Groceries"]) == -40.0


# --- The month a drill-down covers --------------------------------------------


def test_a_month_is_the_month_and_not_a_day_more(db):
    """
    The range is half-open. An inclusive end pulls the first of the next month
    into the bar before it, which is where a rent payment usually sits.
    """
    tx(db, date="2026-07-31", amount=-10.0)
    tx(db, date="2026-08-01", amount=-900.0)

    detail = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-07"))

    assert detail["count"] == 1
    assert detail["net"] == -10.0


def test_december_rolls_into_the_next_year(db):
    """
    Stepping the month index would ask for month 13, which is not a date. This
    is the one month of the year that exercises it.
    """
    tx(db, date="2026-12-15", amount=-10.0)
    tx(db, date="2027-01-02", amount=-900.0)

    detail = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-12"))

    assert detail["count"] == 1


def test_a_month_that_is_not_a_month_is_refused(db):
    """The route validates the shape, and this is the check behind it."""
    with pytest.raises(ValueError, match="YYYY-MM"):
        run(ChartDataService.get_transaction_detail(USER_ID, month="July"))


def test_a_month_wins_over_a_year_because_it_is_more_specific(db):
    """
    Every plot sends both — the month it was clicked on and the year the page
    is set to — so one of them has to lose, and it is the vaguer one.
    """
    tx(db, date="2026-07-03", amount=-40.0)
    tx(db, date="2026-09-03", amount=-90.0)

    detail = run(
        ChartDataService.get_transaction_detail(USER_ID, month="2026-07", year=2026)
    )

    assert detail["count"] == 1


def test_neither_a_month_nor_a_year_covers_all_of_history(db):
    """The dashboard's "All years" is a real scope, not a missing one."""
    tx(db, date="2024-07-03", amount=-40.0)
    tx(db, date="2026-09-03", amount=-90.0)

    assert run(ChartDataService.get_transaction_detail(USER_ID))["count"] == 2


# --- The two sides of a drill-down --------------------------------------------


def test_the_two_directions_come_back_separately(db):
    """
    What was clicked is a net sum, and a refund filed under an expense
    category belongs to it just as much as the spending. Merged into one
    ranking, a single large refund crowds out the payments it offsets.
    """
    tx(db, amount=-40.0)
    tx(db, amount=25.0, minor="Groceries")

    detail = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-07"))

    assert detail["outgoing"]["count"] == 1
    assert detail["incoming"]["count"] == 1
    assert detail["net"] == -15.0


def test_each_side_is_ranked_by_size(db):
    """The panel is a top-N, so the N have to be the largest ones."""
    tx(db, amount=-10.0, booking_text="SMALL")
    tx(db, amount=-90.0, booking_text="BIG")

    outgoing = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-07"))[
        "outgoing"
    ]

    assert [row["amount"] for row in outgoing["transactions"]] == [-90.0, -10.0]


def test_a_side_says_how_much_of_itself_it_is_not_showing(db):
    """
    Otherwise the list implies it is the whole story, and a bar made of forty
    small payments looks like it is made of eight.
    """
    for index in range(5):
        tx(db, amount=-10.0 * (index + 1))

    outgoing = run(
        ChartDataService.get_transaction_detail(USER_ID, month="2026-07", limit=2)
    )["outgoing"]

    assert outgoing["count"] == 5
    assert outgoing["hidden_count"] == 3
    assert outgoing["total"] == -150.0
    assert outgoing["rest"] == -60.0


def test_a_side_with_nothing_on_it_is_still_reported(db):
    """The panel renders both sides unconditionally."""
    tx(db, amount=-40.0)

    incoming = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-07"))[
        "incoming"
    ]

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
            USER_ID, month="2026-07", categories=["Groceries", "Cafés & Snacks"]
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

    row = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-07"))[
        "outgoing"
    ]["transactions"][0]

    assert row["merchant"] == "Spotify AB"
    assert row["booking_text"].startswith("PP.5189.PP")


def test_a_line_with_no_merchant_at_all_is_named_rather_than_blank(db):
    """A blank cell in a list of payments reads as a rendering fault."""
    tx(db, booking_text="")

    row = run(ChartDataService.get_transaction_detail(USER_ID, month="2026-07"))[
        "outgoing"
    ]["transactions"][0]

    assert row["merchant"] == "Unnamed payment"


# --- Comdirect balances -------------------------------------------------------

USER_A = "a" * 24
USER_B = "b" * 24


@pytest.fixture(autouse=True)
def reset_pending():
    """Pending sessions are class state, so they must not leak between tests."""
    original = ComdirectBalanceService._pending
    ComdirectBalanceService._pending = {}
    yield
    ComdirectBalanceService._pending = original


@pytest.fixture(autouse=True)
def credentials_key(monkeypatch):
    """Every save needs a key; tests that don't care about it still get one."""
    from app.config import settings

    monkeypatch.setattr(
        settings, "comdirect_credentials_key", Fernet.generate_key().decode()
    )


def test_status_without_credentials_is_disconnected(db):
    status = run(ComdirectBalanceService.status(USER_A))

    assert status == {
        "has_credentials": False,
        "pending": False,
        "loaded_at": None,
        "balances": None,
    }


def test_saved_credentials_are_encrypted_and_reported_by_status(db):
    run(
        ComdirectBalanceService.save_credentials(
            USER_A, "client", "secret", "12345", "0000"
        )
    )

    stored = db.Credentials.rows[0]
    assert stored.client_id != "client"
    assert stored.pin != "0000"
    assert run(ComdirectBalanceService.status(USER_A))["has_credentials"] is True


def test_saving_credentials_again_replaces_rather_than_duplicates(db):
    run(ComdirectBalanceService.save_credentials(USER_A, "a", "b", "c", "d"))
    run(ComdirectBalanceService.save_credentials(USER_A, "a2", "b2", "c2", "d2"))

    assert len(db.Credentials.rows) == 1


def test_beginning_a_session_without_credentials_is_refused(db):
    with pytest.raises(ValueError):
        run(ComdirectBalanceService.begin_session(USER_A))

    assert ComdirectBalanceService._pending == {}


def test_confirming_with_nothing_pending_is_refused(db):
    with pytest.raises(ValueError):
        run(ComdirectBalanceService.confirm_session(USER_A))


def test_confirming_a_stale_pending_session_is_refused(db):
    ComdirectBalanceService._pending[USER_A] = (
        object(),
        datetime.utcnow() - timedelta(minutes=10),
    )

    with pytest.raises(ValueError):
        run(ComdirectBalanceService.confirm_session(USER_A))


def test_a_successful_confirm_stores_exactly_one_snapshot_for_that_user(
    db, monkeypatch
):
    ComdirectBalanceService._pending[USER_A] = (object(), datetime.utcnow())
    monkeypatch.setattr(
        ComdirectBalanceService,
        "_finish_and_load",
        classmethod(
            lambda cls, auth: {
                "loaded_at": datetime(2026, 9, 1).isoformat(),
                "balances": {"Giro": 1200.0},
            }
        ),
    )

    snapshot = run(ComdirectBalanceService.confirm_session(USER_A))

    assert snapshot["balances"] == {"Giro": 1200.0}
    assert len(db.Snapshot.rows) == 1
    assert db.Snapshot.rows[0].user_id == USER_A
    assert ComdirectBalanceService._pending == {}


def test_status_never_returns_another_users_snapshot(db):
    db.Snapshot.rows.append(
        db.Snapshot(
            user_id=USER_B, loaded_at=datetime(2026, 8, 30), balances={"Giro": 1.0}
        )
    )

    status = run(ComdirectBalanceService.status(USER_A))

    assert status["loaded_at"] is None
    assert status["balances"] is None


def test_the_newest_snapshot_for_the_user_wins(db):
    db.Snapshot.rows.append(
        db.Snapshot(
            user_id=USER_A, loaded_at=datetime(2026, 8, 1), balances={"Giro": 1.0}
        )
    )
    db.Snapshot.rows.append(
        db.Snapshot(
            user_id=USER_A, loaded_at=datetime(2026, 8, 30), balances={"Giro": 2.0}
        )
    )

    status = run(ComdirectBalanceService.status(USER_A))

    assert status["balances"] == {"Giro": 2.0}


# --- Talking to the bank ------------------------------------------------------


def test_the_balances_are_assembled_from_accounts_cards_and_the_depot():
    """
    Three different shapes from the bank's API, flattened into one dictionary
    the dashboard can draw. The depot has no balance of its own — it is the
    sum of the positions in it, which is why it is added up here rather than
    read off a field.
    """

    class Client:
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

    snapshot = ComdirectBalanceService._assemble_balances(Client())

    assert snapshot["balances"] == {
        "Girokonto": 1200.0,
        "Visa": -300.0,
        "Depot": 3000.50,
    }
