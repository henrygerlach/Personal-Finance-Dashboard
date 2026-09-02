"""
Tests for the application's startup and its database connection.

Two small modules with one job each, and both fail in ways that are hard to
see from anywhere else. A document model missing from ``document_models`` does
not raise — Beanie simply never registers the collection, and the first query
against it fails much later with an error naming Beanie rather than the
omission. And a connection opened at startup but never closed at shutdown
leaves the process holding a socket, which nothing in a development loop ever
notices.

Nothing here connects to MongoDB: the motor client and Beanie's initialiser
are both stood in for, and what is checked is what they were handed.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app import database as database_module
from app.database import DatabaseManager


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """A motor client that records the database it was asked for."""

    instances = []

    def __init__(self, url):
        self.url = url
        self.asked_for = []
        self.closed = False
        self.admin = SimpleNamespace(command=self._command)
        self.pinged = []
        FakeClient.instances.append(self)

    def __getitem__(self, name):
        self.asked_for.append(name)
        return SimpleNamespace(name=name)

    async def _command(self, name):
        self.pinged.append(name)
        return {"ok": 1}

    def close(self):
        self.closed = True


@pytest.fixture
def wiring(monkeypatch):
    """The motor client and Beanie's initialiser, stood in for."""
    FakeClient.instances = []
    initialised = []

    async def init_beanie(database, document_models):
        initialised.append((database, document_models))

    monkeypatch.setattr(database_module, "AsyncIOMotorClient", FakeClient)
    monkeypatch.setattr(database_module, "init_beanie", init_beanie)
    return SimpleNamespace(initialised=initialised)


# --- Connecting ---------------------------------------------------------------


def test_the_database_name_comes_from_the_settings(wiring, monkeypatch):
    """
    Never hard-coded, and this is the check that says so. The container holds
    another database belonging to the previous version of this dashboard, and
    a name baked into the code is how work ends up in it.
    """
    monkeypatch.setattr(database_module.settings, "database_name", "bank_analyzer_dev")
    manager = DatabaseManager()

    run(manager.connect())

    assert manager.client.asked_for == ["bank_analyzer_dev"]


def test_the_url_comes_from_the_settings_too(wiring, monkeypatch):
    """
    The host mapping is 27018 so it cannot collide with another project's
    MongoDB, and the default in the code is the standard 27017 — so a run that
    ignored the setting would connect to nothing and fail on the first query.
    """
    monkeypatch.setattr(
        database_module.settings, "mongodb_url", "mongodb://localhost:27018"
    )
    manager = DatabaseManager()

    run(manager.connect())

    assert manager.client.url == "mongodb://localhost:27018"


def test_every_document_model_is_registered(wiring):
    """
    A model left out of this list does not raise. Beanie never registers the
    collection, and the failure arrives much later, at the first query,
    reading as a Beanie problem rather than a missing line here.
    """
    from app.models import (
        CategoryRule,
        ComdirectBalanceSnapshot,
        DismissedTripProposal,
        MerchantCategory,
        PatternDismissal,
        PayPalRecord,
        RecurringOverride,
        Transaction,
        Trip,
        TripAssignment,
    )

    run(DatabaseManager().connect())

    registered = set(wiring.initialised[0][1])
    assert registered == {
        Transaction,
        CategoryRule,
        MerchantCategory,
        ComdirectBalanceSnapshot,
        PayPalRecord,
        RecurringOverride,
        Trip,
        TripAssignment,
        DismissedTripProposal,
        PatternDismissal,
    }


def test_beanie_is_initialised_against_the_database_that_was_opened(
    wiring, monkeypatch
):
    """
    Opening one database and registering the models against another is a
    mistake that produces an app which reads and writes different places.
    """
    monkeypatch.setattr(database_module.settings, "database_name", "bank_analyzer_dev")

    manager = DatabaseManager()
    run(manager.connect())

    assert wiring.initialised[0][0].name == "bank_analyzer_dev"


# --- Disconnecting ------------------------------------------------------------


def test_disconnecting_closes_the_client(wiring):
    """
    A connection opened at startup and never closed leaves the process holding
    a socket, which nothing in a development loop ever notices.
    """
    manager = DatabaseManager()
    run(manager.connect())

    run(manager.disconnect())

    assert manager.client.closed is True


def test_disconnecting_without_having_connected_is_not_an_error(wiring):
    """
    Shutdown runs even when startup failed, and a second exception there hides
    the first one.
    """
    run(DatabaseManager().disconnect())


# --- The health check ---------------------------------------------------------


def test_a_reachable_database_is_healthy(wiring):
    manager = DatabaseManager()
    run(manager.connect())

    assert run(manager.health_check()) is True
    assert manager.client.pinged == ["ping"]


def test_a_database_that_will_not_answer_is_reported_rather_than_raised(wiring):
    """
    The check exists to be asked. Raising would make every caller wrap it, and
    "unhealthy" is the answer, not an accident.
    """
    manager = DatabaseManager()
    run(manager.connect())

    async def refuse(_name):
        raise ConnectionError("no route to host")

    manager.client.admin = SimpleNamespace(command=refuse)

    assert run(manager.health_check()) is False


def test_a_manager_that_never_connected_is_not_healthy(wiring):
    assert run(DatabaseManager().health_check()) is False


def test_the_context_manager_connects_and_always_disconnects(wiring):
    """
    Used by the maintenance scripts, several of which rewrite transactions.
    One that leaves the connection open on failure holds it for the life of
    the process.
    """

    async def use_it():
        async with database_module.get_db_context() as manager:
            assert manager.client is not None
            raise RuntimeError("the script failed")

    with pytest.raises(RuntimeError):
        run(use_it())

    assert FakeClient.instances[-1].closed is True


# --- Startup and shutdown -----------------------------------------------------


def test_the_app_connects_at_startup_and_disconnects_at_shutdown(monkeypatch):
    """
    The lifespan is the only place either happens. A request arriving against
    an unconnected Beanie fails on the collection, not on the connection, so
    the cause is a long way from the symptom.
    """
    from fastapi.testclient import TestClient

    from app import main as main_module

    events = []

    async def connect():
        events.append("connect")

    async def disconnect():
        events.append("disconnect")

    async def balances():
        events.append("balances")

    monkeypatch.setattr(main_module.db_manager, "connect", connect)
    monkeypatch.setattr(main_module.db_manager, "disconnect", disconnect)
    monkeypatch.setattr(
        main_module.ComdirectBalanceService, "initialize_startup_session", balances
    )

    with TestClient(main_module.app):
        assert events == ["connect", "balances"]

    assert events == ["connect", "balances", "disconnect"]


def test_the_balances_are_loaded_after_the_database_is_up(monkeypatch):
    """
    The startup session falls back to a stored snapshot, and reading one needs
    a connection. Loading it first makes the fallback silently unavailable.
    """
    from fastapi.testclient import TestClient

    from app import main as main_module

    order = []

    async def connect():
        order.append("connect")

    async def balances():
        order.append("balances")

    monkeypatch.setattr(main_module.db_manager, "connect", connect)
    monkeypatch.setattr(main_module.db_manager, "disconnect", lambda: _noop())
    monkeypatch.setattr(
        main_module.ComdirectBalanceService, "initialize_startup_session", balances
    )

    with TestClient(main_module.app):
        pass

    assert order.index("connect") < order.index("balances")


async def _noop():
    return None


def test_the_static_files_are_mounted(monkeypatch):
    """
    Every page pulls its stylesheet and its scripts from here. Unmounted, the
    HTML still renders and the page comes up unstyled and inert.
    """
    from app import main as main_module

    assert any(
        getattr(route, "path", None) == "/static" for route in main_module.app.routes
    )


# --- Reading an id off a request ----------------------------------------------


def test_a_good_id_is_parsed():
    """The ordinary case: a 24-character hex string is an id."""
    from app.models import object_id

    assert str(object_id("68af1e2c9b4d5a6f7c8d9e01")) == "68af1e2c9b4d5a6f7c8d9e01"


def test_a_bad_id_is_a_value_error_and_not_a_bson_error():
    """
    This is the whole reason the helper exists. Beanie's PydanticObjectId
    raises bson's InvalidId, which descends from BSONError and not from
    ValueError, so every route that wrapped a lookup in except ValueError let
    a mistyped id through as a 500. The helper reports it the way the routes
    already handle every other unusable value.
    """
    from bson.errors import InvalidId

    from app.models import object_id

    assert not issubclass(InvalidId, ValueError)
    with pytest.raises(ValueError):
        object_id("not-an-id")
