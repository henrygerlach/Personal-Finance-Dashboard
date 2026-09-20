"""
Tests for the HTTP layer.

The routes hold almost no logic of their own, and that is the point: what is
defended here is the wiring. A page route has to reach the right service, put
the badge counts and the active path into the context every template's shared
layout reads, and turn the two failures a service can report — a value it will
not accept, a thing that is not there — into a 422 and a 404 rather than a
500. Those translations are the only error handling a form post ever gets.

The app is built here rather than imported from ``app.main`` so that no
lifespan runs and nothing connects to a database. Templates are stood in for:
what a route owes its template is a context, and the context is what is
checked. Rendering is exercised by the app itself.
"""

import io
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app import routes
from app.auth import current_user
from app.classification.client import ClassificationError
from app.paypal import PayPalFormatError

OID = "68af1e2c9b4d5a6f7c8d9e01"
OTHER_OID = "68af1e2c9b4d5a6f7c8d9e02"
USER_OID = "68af1e2c9b4d5a6f7c8d9e00"


class FakeTemplates:
    """Records the template and context a route asked for."""

    def __init__(self):
        self.rendered = []

    def TemplateResponse(self, name, context):  # noqa: N802 — mirrors Jinja2Templates
        from fastapi.responses import HTMLResponse

        self.rendered.append((name, context))
        return HTMLResponse(f"<html>{name}</html>")


def returning(value):
    """An async stand-in for a service call."""

    async def call(*args, **kwargs):
        return value

    return call


def raising(error):
    """An async stand-in that fails the way a service fails."""

    async def call(*args, **kwargs):
        raise error

    return call


def recording(value=None):
    """An async stand-in that keeps what it was called with."""
    calls = []

    async def call(*args, **kwargs):
        calls.append((args, kwargs))
        return value

    call.calls = calls
    return call


@pytest.fixture
def templates(monkeypatch):
    fake = FakeTemplates()
    monkeypatch.setattr(routes, "templates", fake)
    return fake


@pytest.fixture(autouse=True)
def nav(monkeypatch):
    """
    Every page route spreads the badge counts in, so every page route would
    otherwise need a database.
    """
    monkeypatch.setattr(routes.ReviewService, "count", returning(3))
    monkeypatch.setattr(
        routes.CategorizationService, "get_uncategorized_count", returning(7)
    )


@pytest.fixture
def client():
    """
    The router on a bare app: no lifespan, so no database connection and no
    Comdirect session.
    """
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=USER_OID)
    return TestClient(app)


def context_of(templates, name):
    return next(ctx for template, ctx in templates.rendered if template == name)


@pytest.fixture
def public_client():
    """The unauthenticated router, with a session so login can set one."""
    app = FastAPI()
    app.include_router(routes.public_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return TestClient(app, follow_redirects=False)


# --- The shared layout --------------------------------------------------------


def test_every_page_carries_the_badge_counts(monkeypatch, client, templates):
    """
    The counts belong to the layout, not to any one page. Passing them per
    route made them vanish on whichever page forgot: an undefined count
    renders as nothing rather than failing, so the omission looked exactly
    like an empty queue.
    """
    monkeypatch.setattr(routes.ReviewService, "queue", returning([]))
    monkeypatch.setattr(routes.ReviewService, "paypal_context", returning({}))
    monkeypatch.setattr(routes.RecurringService, "overview", returning({}))
    monkeypatch.setattr(routes.PatternService, "overview", returning({}))
    monkeypatch.setattr(routes.MerchantService, "overview", returning({}))
    monkeypatch.setattr(routes.TripService, "overview", returning({}))
    monkeypatch.setattr(routes.TripService, "proposals", returning([]))
    monkeypatch.setattr(
        routes.ComdirectBalanceService, "should_render_balance_section", lambda: False
    )

    for path in (
        "/",
        "/upload",
        "/review",
        "/recurring",
        "/patterns",
        "/merchants",
        "/trips",
    ):
        assert client.get(path).status_code == 200

    for _, context in templates.rendered:
        assert context["review_count"] == 3
        assert context["uncategorized_count"] == 7


def test_each_page_names_the_navigation_entry_it_belongs_to(
    monkeypatch, client, templates
):
    """
    ``active_path`` is what the top bar highlights. A trip detail page belongs
    under Trips rather than to itself, or opening one drops the highlight.
    """
    monkeypatch.setattr(routes.TripService, "overview", returning({}))
    monkeypatch.setattr(routes.TripService, "proposals", returning([]))
    monkeypatch.setattr(routes.TripService, "detail", returning({"name": "Lisbon"}))

    client.get("/trips")
    client.get(f"/trips/{OID}")

    assert context_of(templates, "trips.html")["active_path"] == "/trips"
    assert context_of(templates, "trip.html")["active_path"] == "/trips"


# --- Cache-busting static URLs ------------------------------------------------


def test_a_static_url_carries_the_file_s_modification_time():
    """
    Starlette sends no Cache-Control, and Chrome's own heuristic is to hold a
    script for the session without revalidating — so an edited script silently
    does not reach the page. A changed file has to be a different URL.
    """
    url = routes.static_url("styles.css")

    assert url.startswith("/static/styles.css?v=")
    assert url.split("=")[1].isdigit()


def test_a_missing_static_file_still_produces_a_url():
    """
    A missing asset is the server's problem to report as a 404, not something
    to fail template rendering over.
    """
    assert routes.static_url("not-there.js") == "/static/not-there.js"


# --- The review queue ---------------------------------------------------------


def test_the_review_page_asks_for_one_screenful(monkeypatch, client, templates):
    """The queue is paged; asking for all of it on every load is not free."""
    queue = recording([])
    monkeypatch.setattr(routes.ReviewService, "queue", queue)
    monkeypatch.setattr(routes.ReviewService, "paypal_context", returning({}))

    client.get("/review?skip=50")

    assert queue.calls[0][1] == {"limit": 50, "skip": 50}
    assert context_of(templates, "review.html")["skip"] == 50


def test_a_negative_skip_is_refused(client):
    """
    The offset reaches a database query. Letting a negative one through is a
    query nobody meant to write.
    """
    assert client.get("/review?skip=-1").status_code == 422


def test_correcting_a_category_redirects_back_to_the_queue(monkeypatch, client):
    """
    Filing one transaction should leave the person on the next one, not on a
    result page they have to navigate away from.
    """
    correct = recording()
    monkeypatch.setattr(routes.ReviewService, "correct", correct)

    reply = client.post(
        f"/review/{OID}",
        data={"major_category": "Food & Drink", "minor_category": "Groceries"},
        follow_redirects=False,
    )

    assert reply.status_code == 303
    assert reply.headers["location"] == "/review"
    assert correct.calls[0][0] == (USER_OID, OID, "Food & Drink", "Groceries")


def test_a_correction_the_service_will_not_accept_is_a_422(monkeypatch, client):
    """
    Covers both an unknown transaction and a pair that is not in the
    hierarchy. Either way there is nothing to save, and a 500 would tell the
    user their browser was at fault.
    """
    monkeypatch.setattr(
        routes.ReviewService, "correct", raising(ValueError("no such category"))
    )

    reply = client.post(
        f"/review/{OID}",
        data={"major_category": "Nonsense", "minor_category": "Nonsense"},
    )

    assert reply.status_code == 422
    assert "no such category" in reply.json()["detail"]


def test_a_correction_missing_a_field_is_refused(client):
    """A form post is a public surface, whatever the page sends."""
    assert (
        client.post(
            f"/review/{OID}", data={"major_category": "Food & Drink"}
        ).status_code
        == 422
    )


# --- Recurring ----------------------------------------------------------------


def test_detection_reports_what_it_found(monkeypatch, client):
    """The page shows the count, so the run has to come back with one."""
    runs = [SimpleNamespace(occurrences=12), SimpleNamespace(occurrences=4)]
    monkeypatch.setattr(routes.RecurringService, "detect_and_store", returning(runs))

    body = client.post("/api/recurring/detect").json()

    assert body == {"status": "success", "schedules": 2, "transactions": 16}


def test_an_override_is_stored_and_echoed(monkeypatch, client):
    """
    The page updates in place from the reply rather than reloading, so what
    was stored has to come back.
    """
    monkeypatch.setattr(
        routes.RecurringService, "set_override", returning({"status_value": "ended"})
    )

    body = client.post(
        "/api/recurring/override",
        data={"schedule_id": "NETFLIX|monthly", "decision": "ended"},
    ).json()

    assert body == {"status": "success", "status_value": "ended"}


def test_an_unknown_decision_is_a_422(monkeypatch, client):
    """
    Covers an unknown decision and a schedule detection no longer produces.
    Neither is something to store.
    """
    monkeypatch.setattr(
        routes.RecurringService, "set_override", raising(ValueError("unknown decision"))
    )

    reply = client.post(
        "/api/recurring/override", data={"schedule_id": "X", "decision": "maybe"}
    )

    assert reply.status_code == 422


def test_an_override_can_be_handed_back_to_the_detector(monkeypatch, client):
    """A decision the user regrets has to be reversible."""
    monkeypatch.setattr(routes.RecurringService, "clear_override", returning(True))

    body = client.post(
        "/api/recurring/override/clear", data={"schedule_id": "X"}
    ).json()

    assert body == {"status": "success", "cleared": True}


def test_clearing_an_override_that_is_not_there_is_a_422(monkeypatch, client):
    """Nothing to clear is a bad request, not a server error."""
    monkeypatch.setattr(
        routes.RecurringService, "clear_override", raising(ValueError("no override"))
    )

    assert (
        client.post(
            "/api/recurring/override/clear", data={"schedule_id": "X"}
        ).status_code
        == 422
    )


# --- Patterns -----------------------------------------------------------------


def test_a_finding_is_dismissed_on_its_own_facts(monkeypatch, client):
    """
    Findings are recomputed on every load, so the dismissal is keyed on what
    the finding is rather than on a handle the page carries. All three parts
    have to reach the service or the wrong finding is put away.
    """
    dismiss = recording(True)
    monkeypatch.setattr(routes.PatternService, "dismiss", dismiss)

    body = client.post(
        "/api/patterns/dismiss",
        data={"kind": "drift", "subject": "Cafés & Snacks", "month": "2026-07"},
    ).json()

    assert body == {"status": "success", "stored": True}
    assert dismiss.calls[0][0] == (USER_OID, "drift", "Cafés & Snacks", "2026-07")


def test_a_finding_about_the_whole_ledger_needs_no_subject(monkeypatch, client):
    """
    Some findings concern no single category and no single month. Requiring
    either would make them undismissable.
    """
    dismiss = recording(True)
    monkeypatch.setattr(routes.PatternService, "dismiss", dismiss)

    client.post("/api/patterns/dismiss", data={"kind": "total_drift"})

    assert dismiss.calls[0][0] == (USER_OID, "total_drift", "", "")


def test_a_dismissal_the_service_rejects_is_a_422(monkeypatch, client):
    monkeypatch.setattr(
        routes.PatternService, "dismiss", raising(ValueError("bad kind"))
    )

    assert (
        client.post("/api/patterns/dismiss", data={"kind": "nonsense"}).status_code
        == 422
    )


def test_a_dismissed_finding_can_be_restored(monkeypatch, client):
    monkeypatch.setattr(routes.PatternService, "restore", returning(True))

    body = client.post("/api/patterns/restore", data={"kind": "drift"}).json()

    assert body == {"status": "success", "restored": True}


# --- Trips: the form values ---------------------------------------------------


def test_a_trip_is_created_and_opened_where_its_queue_is(monkeypatch, client):
    """
    Creating a trip is only half the job; the transactions have to be
    confirmed, and that queue is on the trip's own page.
    """
    monkeypatch.setattr(
        routes.TripService, "create", returning(SimpleNamespace(id=OID))
    )

    reply = client.post(
        "/trips",
        data={"name": "Lisbon", "start_date": "2026-07-01", "end_date": "2026-07-08"},
        follow_redirects=False,
    )

    assert reply.status_code == 303
    assert reply.headers["location"] == f"/trips/{OID}"


def test_a_date_the_form_should_never_send_is_refused(client):
    """
    The date inputs make this unlikely and a form post is a public surface.
    The message names which of the two fields was wrong.
    """
    reply = client.post(
        "/trips",
        data={"name": "Lisbon", "start_date": "not-a-date", "end_date": "2026-07-08"},
    )

    assert reply.status_code == 422
    assert reply.json()["detail"] == "Start date is not a date"


def test_a_budget_written_with_a_comma_is_understood(monkeypatch, client):
    """
    The app is used in a locale where the decimal separator is a comma, and
    the field is free text. Read with the C locale, "1200,50" is not a number.
    """
    create = recording(SimpleNamespace(id=OID))
    monkeypatch.setattr(routes.TripService, "create", create)

    client.post(
        "/trips",
        data={
            "name": "Lisbon",
            "start_date": "2026-07-01",
            "end_date": "2026-07-08",
            "budget": "1200,50",
        },
        follow_redirects=False,
    )

    assert create.calls[0][1]["budget"] == 1200.50


def test_a_negative_budget_is_stored_as_an_amount(monkeypatch, client):
    """
    A budget is a size, not a direction. Spending is negative everywhere else
    in the app, and somebody typing the minus sign in is not unreasonable.
    """
    create = recording(SimpleNamespace(id=OID))
    monkeypatch.setattr(routes.TripService, "create", create)

    client.post(
        "/trips",
        data={
            "name": "Lisbon",
            "start_date": "2026-07-01",
            "end_date": "2026-07-08",
            "budget": "-800",
        },
        follow_redirects=False,
    )

    assert create.calls[0][1]["budget"] == 800.0


def test_an_empty_budget_is_no_budget_rather_than_zero(monkeypatch, client):
    """
    A trip with no budget must not render as one that has been overspent from
    the first euro.
    """
    create = recording(SimpleNamespace(id=OID))
    monkeypatch.setattr(routes.TripService, "create", create)

    client.post(
        "/trips",
        data={
            "name": "Lisbon",
            "start_date": "2026-07-01",
            "end_date": "2026-07-08",
            "budget": "   ",
        },
        follow_redirects=False,
    )

    assert create.calls[0][1]["budget"] is None


def test_a_budget_that_is_not_a_number_is_refused(client):
    assert (
        client.post(
            "/trips",
            data={
                "name": "Lisbon",
                "start_date": "2026-07-01",
                "end_date": "2026-07-08",
                "budget": "lots",
            },
        ).status_code
        == 422
    )


def test_a_trip_the_service_will_not_create_is_a_422(monkeypatch, client):
    """An end date before the start is the service's rule, not the route's."""
    monkeypatch.setattr(
        routes.TripService, "create", raising(ValueError("ends before it starts"))
    )

    assert (
        client.post(
            "/trips",
            data={"name": "X", "start_date": "2026-07-08", "end_date": "2026-07-01"},
        ).status_code
        == 422
    )


# --- Trips: reading, editing and deleting -------------------------------------


def test_a_trip_that_does_not_exist_is_a_404(monkeypatch, client):
    """
    A deleted trip is a stale bookmark, and a stale bookmark is not a server
    error.
    """
    monkeypatch.setattr(routes.TripService, "detail", raising(ValueError("gone")))

    assert client.get(f"/trips/{OID}").status_code == 404


def test_editing_a_trip_returns_to_that_trip(monkeypatch, client):
    monkeypatch.setattr(routes.TripService, "update", recording())

    reply = client.post(
        f"/trips/{OID}/edit",
        data={"name": "Lisbon", "start_date": "2026-07-01", "end_date": "2026-07-09"},
        follow_redirects=False,
    )

    assert reply.headers["location"] == f"/trips/{OID}"


def test_deleting_a_trip_returns_to_the_list(monkeypatch, client):
    """The trip that was being looked at is gone, so there is nowhere else."""
    monkeypatch.setattr(routes.TripService, "delete", recording())

    reply = client.post(f"/trips/{OID}/delete", follow_redirects=False)

    assert reply.status_code == 303
    assert reply.headers["location"] == "/trips"


def test_deleting_a_trip_that_is_not_there_is_a_404(monkeypatch, client):
    monkeypatch.setattr(routes.TripService, "delete", raising(ValueError("gone")))

    assert client.post(f"/trips/{OID}/delete").status_code == 404


# --- Trips: assignment --------------------------------------------------------


def test_assigning_a_transaction_returns_the_recomputed_totals(monkeypatch, client):
    """
    The queue is worked through one line at a time. Reloading the page after
    every decision would make it unusable, so the totals come back instead.
    """
    monkeypatch.setattr(routes.TripService, "assign", returning({"total": -812.40}))

    body = client.post(
        f"/api/trips/{OID}/assign",
        data={"transaction_id": OTHER_OID, "decision": "included"},
    ).json()

    assert body == {"status": "success", "total": -812.40}


def test_an_empty_decision_hands_the_transaction_back_to_the_suggester(
    monkeypatch, client
):
    """
    Undoing a decision is not the same as deciding "excluded": the first
    returns the line to the queue, the second answers it.
    """
    clear = recording({"total": 0.0})
    monkeypatch.setattr(routes.TripService, "clear", clear)
    monkeypatch.setattr(
        routes.TripService, "assign", raising(AssertionError("not this one"))
    )

    client.post(
        f"/api/trips/{OID}/assign", data={"transaction_id": OTHER_OID, "decision": ""}
    )

    assert clear.calls[0][0] == (USER_OID, OID, OTHER_OID)


def test_a_share_is_passed_through_for_a_booking_somebody_else_was_on(
    monkeypatch, client
):
    """A house booked for six is not this trip's cost six times over."""
    assign = recording({"total": 0.0})
    monkeypatch.setattr(routes.TripService, "assign", assign)

    client.post(
        f"/api/trips/{OID}/assign",
        data={"transaction_id": OTHER_OID, "decision": "included", "share": "0.5"},
    )

    assert assign.calls[0][1]["share"] == 0.5


def test_an_assignment_the_service_rejects_is_a_422(monkeypatch, client):
    monkeypatch.setattr(
        routes.TripService, "assign", raising(ValueError("unknown decision"))
    )

    assert (
        client.post(
            f"/api/trips/{OID}/assign",
            data={"transaction_id": OTHER_OID, "decision": "perhaps"},
        ).status_code
        == 422
    )


def test_the_split_is_one_number_for_the_whole_trip(monkeypatch, client):
    """
    Splitting is a fact about the trip. Applied per payment it would overwrite
    the shares set by hand with no way back.
    """
    split = recording({"total": -406.20})
    monkeypatch.setattr(routes.TripService, "set_split", split)

    body = client.post(f"/api/trips/{OID}/split", data={"ways": "2"}).json()

    assert body == {"status": "success", "total": -406.20}
    assert split.calls[0][0] == (USER_OID, OID, 2)


def test_a_split_the_service_rejects_is_a_422(monkeypatch, client):
    monkeypatch.setattr(
        routes.TripService, "set_split", raising(ValueError("at least one"))
    )

    assert client.post(f"/api/trips/{OID}/split", data={"ways": "0"}).status_code == 422


def test_the_selected_transactions_are_taken_as_given(monkeypatch, client):
    """
    The route writes the ids the page had selected rather than re-deriving the
    detector's suggestions, so what was ticked and what is written agree even
    if detection has moved on.
    """
    assign_many = recording(2)
    monkeypatch.setattr(routes.TripService, "assign_many", assign_many)

    body = client.post(
        f"/api/trips/{OID}/assign-selected",
        data={"transaction_id": [OTHER_OID, OID]},
    ).json()

    assert body == {"status": "success", "assigned": 2}
    assert assign_many.calls[0][0][2] == [OTHER_OID, OID]


def test_searching_for_a_transaction_to_attach_by_hand(monkeypatch, client):
    """
    The suggester's windows are deliberately narrow, so there has to be a way
    to reach a deposit paid two years out.
    """
    monkeypatch.setattr(routes.TripService, "search", returning([{"id": OTHER_OID}]))

    body = client.get(f"/api/trips/{OID}/search?q=ryanair").json()

    assert body == {
        "status": "success",
        "query": "ryanair",
        "results": [{"id": OTHER_OID}],
    }


def test_a_one_character_search_is_refused(client):
    """
    A single letter matches most of a ledger. The floor is in the route
    because it protects the query, not the presentation.
    """
    assert client.get(f"/api/trips/{OID}/search?q=r").status_code == 422


def test_searching_within_a_trip_that_is_gone_is_a_404(monkeypatch, client):
    monkeypatch.setattr(routes.TripService, "search", raising(ValueError("gone")))

    assert client.get(f"/api/trips/{OID}/search?q=ryanair").status_code == 404


# --- Analytics ----------------------------------------------------------------


def test_the_chart_endpoint_passes_its_filters_through(monkeypatch, client):
    """
    Categories are repeatable because a breakdown's "N smaller categories"
    wedge stands for the whole tail it collapsed and opens like any other.
    """
    chart = recording({"months": []})
    monkeypatch.setattr(routes.ChartDataService, "get_chart_data", chart)

    client.get(
        "/api/chart-data?year=2026&categories=Groceries&categories=Rent&category_type=minor"
    )

    assert chart.calls[0][1] == {
        "year": 2026,
        "categories": ["Groceries", "Rent"],
        "category_type": "minor",
    }


def test_the_chart_endpoint_defaults_to_no_filter(monkeypatch, client):
    """Opening the dashboard asks for everything."""
    chart = recording({"months": []})
    monkeypatch.setattr(routes.ChartDataService, "get_chart_data", chart)

    client.get("/api/chart-data")

    assert chart.calls[0][1] == {"year": None, "categories": [], "category_type": None}


def test_the_cashflow_endpoint_passes_the_year_through(monkeypatch, client):
    cashflow = recording({"months": []})
    monkeypatch.setattr(routes.ChartDataService, "get_cashflow_data", cashflow)

    client.get("/api/cashflow-data?year=2026")

    assert cashflow.calls[0][1] == {"year": 2026}


def test_a_month_must_be_a_real_month(client):
    """
    The month reaches a range query. "2026-13" is not a month and would
    silently match nothing rather than fail.
    """
    assert client.get("/api/transaction-detail?month=2026-13").status_code == 422
    assert client.get("/api/transaction-detail?month=July").status_code == 422


def test_a_well_formed_month_is_accepted(monkeypatch, client):
    detail = recording({"total": 0})
    monkeypatch.setattr(routes.ChartDataService, "get_transaction_detail", detail)

    assert client.get("/api/transaction-detail?month=2026-07").status_code == 200
    assert detail.calls[0][1]["month"] == "2026-07"


def test_the_drill_down_limit_is_bounded(monkeypatch, client):
    """
    The popup shows a top-N. An unbounded limit is a way to ask for the whole
    ledger through a chart click.
    """
    monkeypatch.setattr(
        routes.ChartDataService, "get_transaction_detail", returning({})
    )

    assert client.get("/api/transaction-detail?limit=0").status_code == 422
    assert client.get("/api/transaction-detail?limit=51").status_code == 422
    assert client.get("/api/transaction-detail?limit=50").status_code == 200


def test_the_balances_come_from_the_startup_snapshot(monkeypatch, client):
    """
    The Comdirect session is optional and authenticated at startup; the page
    must not try to open one per request.
    """
    monkeypatch.setattr(
        routes.ComdirectBalanceService,
        "get_balances_snapshot",
        lambda: {"accounts": [], "status": "disabled"},
    )

    assert client.get("/api/account-balances").json()["status"] == "disabled"


# --- Classification -----------------------------------------------------------


class FakeQuery:
    """The find().sort().limit().to_list() chain, without a database."""

    def __init__(self, items):
        self.items = items

    def sort(self, *args):
        return self

    def limit(self, *args):
        return self

    async def to_list(self):
        return self.items


class FakeTransactionModel:
    """
    A stand-in for the document.

    The field names have to exist as class attributes: the routes build their
    queries as ``Transaction.hash == ...``, and on the real document those are
    Beanie expression fields that refuse to be read before a collection
    exists.
    """

    hash = "hash"
    classification_source = "classification_source"
    user_id = "user_id"
    found = []

    @classmethod
    def find(cls, *args, **kwargs):
        return FakeQuery(cls.found)

    @classmethod
    async def get(cls, *args, **kwargs):
        return cls.found[0] if cls.found else None


@pytest.fixture
def transactions(monkeypatch):
    """The document stood in for, with no rows unless a test adds them."""

    class Model(FakeTransactionModel):
        found = []

    monkeypatch.setattr(routes, "Transaction", Model)
    return Model


def fake_run(**extra):
    defaults = dict(
        classifications=[],
        cache_hits=0,
        api_calls=0,
        fallbacks=0,
        below_threshold=0,
        invalid_pairs=0,
        failed=0,
        usage={},
    )
    defaults.update(extra)
    return SimpleNamespace(**defaults)


def test_classifying_nothing_says_so_rather_than_calling_the_api(client, transactions):
    """
    An import where the cache answered everything reaches this with an empty
    list, and it must not become a request.
    """
    assert client.post("/api/classify").json() == {
        "status": "empty",
        "classified": 0,
        "results": [],
    }


def test_a_run_reports_its_summary_and_its_results(monkeypatch, client, transactions):
    """
    The summary is how a person checks the funnel is behaving — that the cache
    is answering most of it and the API is the remainder.
    """
    classification = SimpleNamespace(
        transaction_id=OID,
        merchant="ALDI",
        major="Food & Drink",
        minor="Groceries",
        flow_type="expense",
        confidence=0.912345,
        reason="",
        source="llm",
        needs_review=False,
    )
    transactions.found = [SimpleNamespace(id=OID)]
    monkeypatch.setattr(
        routes.ClassificationService,
        "classify",
        returning(
            fake_run(classifications=[classification], cache_hits=4, api_calls=1)
        ),
    )

    body = client.post("/api/classify").json()

    assert body["status"] == "success"
    assert body["classified"] == 1
    assert body["summary"]["cache_hits"] == 4
    assert body["results"][0]["confidence"] == 0.912
    assert body["persisted"] == 0


def test_a_run_writes_nothing_unless_it_is_asked_to(monkeypatch, client, transactions):
    """
    Persisting is off by default so a run can be inspected before it changes
    anything.
    """
    written = recording(5)
    transactions.found = [SimpleNamespace(id=OID)]
    monkeypatch.setattr(routes.ClassificationService, "classify", returning(fake_run()))
    monkeypatch.setattr(routes, "apply", written)

    client.post("/api/classify")
    assert written.calls == []

    assert client.post("/api/classify?persist=true").json()["persisted"] == 5


def test_a_missing_api_key_is_a_503_not_a_500(monkeypatch, client, transactions):
    """
    The service is unavailable rather than broken, and the distinction is what
    tells the person to go and set the key.
    """
    transactions.found = [SimpleNamespace(id=OID)]
    monkeypatch.setattr(
        routes.ClassificationService,
        "classify",
        raising(ClassificationError("ANTHROPIC_API_KEY is not set")),
    )

    reply = client.post("/api/classify")

    assert reply.status_code == 503
    assert "ANTHROPIC_API_KEY" in reply.json()["detail"]


def test_the_classify_limit_is_bounded(client):
    """A limit reaching a query has to have an end."""
    assert client.post("/api/classify?limit=0").status_code == 422
    assert client.post("/api/classify?limit=501").status_code == 422


# --- Upload -------------------------------------------------------------------


@pytest.fixture
def upload(monkeypatch):
    """The upload path with its database and its parser stood in for."""
    inserted = []

    class Model(FakeTransactionModel):
        found = []

    class FakeRule:
        user_id = "user_id"

        @staticmethod
        def find(*args, **kwargs):
            return FakeQuery([])

    monkeypatch.setattr(routes, "Transaction", Model)
    monkeypatch.setattr(routes, "CategoryRule", FakeRule)
    monkeypatch.setattr(routes, "_classify_import", returning({"classification": {}}))

    def parser(rules):
        return SimpleNamespace(
            parse=lambda contents: [
                SimpleNamespace(hash="h1", insert=lambda: _record(inserted, "h1"))
            ]
        )

    monkeypatch.setattr(routes, "Parser", parser)
    monkeypatch.setattr(routes, "looks_like_export", lambda contents: False)
    return SimpleNamespace(inserted=inserted, monkeypatch=monkeypatch, model=Model)


def _record(store, value):
    async def call():
        store.append(value)

    return call()


def csv_file(name="umsaetze.csv", body=b"Buchungstag;\n"):
    return {"files": (name, io.BytesIO(body), "text/csv")}


def test_a_statement_is_imported(client, upload):
    body = client.post("/upload", files=csv_file()).json()

    assert body["status"] == "success"
    assert body["imported"] == 1
    assert upload.inserted == ["h1"]


def test_a_transaction_already_in_the_database_is_skipped(client, upload):
    """
    Monthly exports overlap by design; re-importing one must not double every
    transaction in the overlap.
    """
    upload.model.found = [SimpleNamespace(hash="h1")]

    body = client.post("/upload", files=csv_file()).json()

    assert body["imported"] == 0
    assert body["files"][0]["skipped"] == 1
    assert upload.inserted == []


def test_only_csv_files_are_accepted(client, upload):
    """
    The parser would fail on anything else anyway; failing here names the file
    instead of reporting a parse error from inside pandas.
    """
    reply = client.post(
        "/upload",
        files={"files": ("statement.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
    )

    assert reply.status_code == 400
    assert "statement.pdf" in reply.json()["detail"]


def test_a_paypal_export_goes_to_the_ledger_instead_of_the_parser(client, upload):
    """
    A PayPal export is not a statement and holds no transactions of its own.
    One upload control takes both, so the file has to be recognised rather
    than parsed as a statement and rejected.
    """
    upload.monkeypatch.setattr(routes, "looks_like_export", lambda contents: True)
    upload.monkeypatch.setattr(
        routes,
        "import_export",
        returning(
            SimpleNamespace(added=12, already_known=3, summary=lambda: "12 added")
        ),
    )

    body = client.post("/upload", files=csv_file(name="Download.CSV")).json()

    assert body["paypal_rows"] == 12
    assert body["files"][0]["kind"] == "paypal"
    assert body["imported"] == 0
    assert upload.inserted == []


def test_an_unreadable_paypal_export_names_the_file(client, upload):
    upload.monkeypatch.setattr(routes, "looks_like_export", lambda contents: True)
    upload.monkeypatch.setattr(
        routes, "import_export", raising(PayPalFormatError("no amount column"))
    )

    reply = client.post("/upload", files=csv_file(name="Download.CSV"))

    assert reply.status_code == 422
    assert "Download.CSV" in reply.json()["detail"]


def test_a_parsing_failure_names_the_file(client, upload):
    """
    Several files go up at once, so "parsing error" without a name leaves the
    person to work out which of them was wrong.
    """

    def exploding(rules):
        return SimpleNamespace(parse=_raise)

    def _raise(contents):
        raise ValueError("column missing")

    upload.monkeypatch.setattr(routes, "Parser", exploding)

    reply = client.post("/upload", files=csv_file(name="broken.csv"))

    assert reply.status_code == 422
    assert "broken.csv" in reply.json()["detail"]


def test_an_import_that_added_nothing_does_not_classify(client, upload):
    """
    Classification costs money. A re-imported file that was entirely duplicate
    must not pay for it again.
    """
    upload.model.found = [SimpleNamespace(hash="h1")]
    classify = recording({"classification": {}})
    upload.monkeypatch.setattr(routes, "_classify_import", classify)

    client.post("/upload", files=csv_file())

    assert classify.calls == []


# --- Classification during import ---------------------------------------------


def test_a_classification_failure_does_not_fail_the_import(monkeypatch):
    """
    The transactions are already stored by the time this runs. Letting the
    exception out would report a failed upload for data that is safely in the
    database.
    """
    import asyncio

    monkeypatch.setattr(
        routes.ClassificationService, "classify", raising(RuntimeError("boom"))
    )

    result = asyncio.run(routes._classify_import(USER_OID, [SimpleNamespace(id=OID)]))

    assert result["classification"]["status"] == "failed"
    assert "boom" in result["classification"]["detail"]


def test_a_missing_key_during_import_is_reported_as_unavailable(monkeypatch):
    """
    Distinguished from a failure because there is nothing wrong with the data
    — the run simply could not happen, and the transactions keep their
    provisional category until it can.
    """
    import asyncio

    monkeypatch.setattr(
        routes.ClassificationService, "classify", raising(ClassificationError("no key"))
    )

    result = asyncio.run(routes._classify_import(USER_OID, [SimpleNamespace(id=OID)]))

    assert result["classification"]["status"] == "unavailable"


def test_the_comparison_log_is_only_written_while_both_systems_run(monkeypatch):
    """
    A log of the classifier agreeing with itself is noise. It is only evidence
    while the rule engine is still producing a verdict to disagree with.
    """
    import asyncio

    monkeypatch.setattr(routes.ClassificationService, "classify", returning(fake_run()))
    monkeypatch.setattr(routes, "apply", returning(1))
    monkeypatch.setattr(routes, "report_lines", lambda run, total: [])
    written = []
    monkeypatch.setattr(
        routes, "write_comparison", lambda t, c: written.append(1) or ("p", 1, 0)
    )

    monkeypatch.setattr(routes.settings, "comparison_mode_enabled", True)
    monkeypatch.setattr(routes.settings, "rules_enabled", False)
    asyncio.run(routes._classify_import(USER_OID, [SimpleNamespace(id=OID)]))
    assert written == []

    monkeypatch.setattr(routes.settings, "rules_enabled", True)
    asyncio.run(routes._classify_import(USER_OID, [SimpleNamespace(id=OID)]))
    assert written == [1]


def test_nothing_is_persisted_while_the_rule_engine_is_in_charge(monkeypatch):
    """
    The switch decides whose verdict is stored. With it off the log still
    records both, which is the whole point of running them side by side.
    """
    import asyncio

    monkeypatch.setattr(routes.ClassificationService, "classify", returning(fake_run()))
    monkeypatch.setattr(routes, "report_lines", lambda run, total: [])
    monkeypatch.setattr(routes.settings, "comparison_mode_enabled", False)
    monkeypatch.setattr(routes.settings, "classification_enabled", False)
    monkeypatch.setattr(routes, "apply", raising(AssertionError("must not write")))

    result = asyncio.run(routes._classify_import(USER_OID, [SimpleNamespace(id=OID)]))

    assert result["classification"]["persisted"] == 0


def test_a_detected_trip_proposal_can_be_refused(monkeypatch, client):
    """
    Detection is recomputed on every load, so without a stored refusal the
    same wrongly flagged week of online shopping comes back after every upload.
    """
    dismiss = recording(True)
    monkeypatch.setattr(routes.TripService, "dismiss_proposal", dismiss)

    body = client.post(f"/api/trips/proposals/{OID}/dismiss").json()

    assert body == {"status": "success", "dismissed": True}
    assert dismiss.calls[0][0] == (USER_OID, OID)


def test_refusing_a_proposal_detection_no_longer_makes_is_a_422(monkeypatch, client):
    """A refusal of nothing is a row that can never be matched or undone."""
    monkeypatch.setattr(
        routes.TripService, "dismiss_proposal", raising(ValueError("no such proposal"))
    )

    assert client.post(f"/api/trips/proposals/{OID}/dismiss").status_code == 422


def test_a_refused_proposal_can_be_restored(monkeypatch, client):
    monkeypatch.setattr(routes.TripService, "restore_proposal", returning(True))

    body = client.post(f"/api/trips/proposals/{OID}/restore").json()

    assert body == {"status": "success", "restored": True}


def test_editing_a_trip_the_service_rejects_is_a_422(monkeypatch, client):
    """The same rules as creating one, reached through a different route."""
    monkeypatch.setattr(
        routes.TripService, "update", raising(ValueError("needs a name"))
    )

    reply = client.post(
        f"/trips/{OID}/edit",
        data={"name": "", "start_date": "2026-07-01", "end_date": "2026-07-08"},
    )

    assert reply.status_code == 422


def test_an_edit_with_an_impossible_date_is_refused_before_the_service(client):
    """The route parses the two dates, so a bad one never reaches the service."""
    reply = client.post(
        f"/trips/{OID}/edit",
        data={"name": "Lisbon", "start_date": "2026-07-01", "end_date": "31/07/2026"},
    )

    assert reply.status_code == 422
    assert reply.json()["detail"] == "End date is not a date"


def test_assigning_a_selection_the_service_rejects_is_a_422(monkeypatch, client):
    monkeypatch.setattr(
        routes.TripService, "assign_many", raising(ValueError("no such trip"))
    )

    reply = client.post(
        f"/api/trips/{OID}/assign-selected", data={"transaction_id": [OTHER_OID]}
    )

    assert reply.status_code == 422


def test_a_classify_run_can_name_the_transactions_it_should_cover(
    monkeypatch, client, transactions
):
    """
    The default is whatever is still awaiting classification; naming ids is
    how the backfill script and the review page re-run one line.
    """
    transactions.found = [SimpleNamespace(id=OID, user_id=USER_OID)]
    monkeypatch.setattr(routes.ClassificationService, "classify", returning(fake_run()))

    body = client.post("/api/classify", json=[OID]).json()

    assert body["status"] == "success"


# --- An id that is not an id --------------------------------------------------
#
# Ids arrive in paths and form posts, so anything at all can be sent. Beanie's
# PydanticObjectId raises bson's InvalidId, which is not a ValueError, so it
# slips past the except ValueError a route wraps its lookup in and leaves as a
# 500 — the one answer that says the server is broken rather than the request.


def test_deleting_a_rule_by_an_id_that_is_not_an_id_is_a_404(monkeypatch, client):
    """A mistyped id names no rule, which is what "not found" is for."""

    class FakeRule:
        @staticmethod
        async def get(*args, **kwargs):
            raise AssertionError("the id should never have reached the database")

    monkeypatch.setattr(routes, "CategoryRule", FakeRule)

    assert client.post("/rules/not-an-id/delete").status_code == 404


def test_saving_a_rule_against_an_id_that_is_not_an_id_is_a_404(monkeypatch, client):
    """The same for the form the categorize page posts."""

    class Model(FakeTransactionModel):
        @classmethod
        async def get(cls, *args, **kwargs):
            raise AssertionError("the id should never have reached the database")

    monkeypatch.setattr(routes, "Transaction", Model)

    reply = client.post(
        "/categorize",
        data={
            "transaction_id": "not-an-id",
            "substring": "AMAZON",
            "major_category": "Shopping",
            "minor_category": "General Merchandise",
        },
    )

    assert reply.status_code == 404


def test_classifying_an_id_that_is_not_an_id_leaves_it_out(monkeypatch, client):
    """
    The endpoint takes a list in the body, and one bad id in it must not fail
    the others. An unparseable id names no transaction, which is the answer an
    id naming a deleted one already gets: it is missing from the results.
    """

    class Model(FakeTransactionModel):
        asked = []

        @classmethod
        async def get(cls, value, *args, **kwargs):
            cls.asked.append(value)
            return None

    monkeypatch.setattr(routes, "Transaction", Model)

    reply = client.post("/api/classify", json=["not-an-id", OID])

    assert reply.status_code == 200
    assert reply.json()["status"] == "empty"
    assert [str(value) for value in Model.asked] == [OID]


def test_a_statement_is_checked_for_duplicates_in_one_query(client, upload):
    """
    Monthly exports overlap, so most rows in a file are usually already
    stored. Asking about each row on its own made the import cost one round
    trip per line of the statement; the whole file is one question.
    """
    queries = []

    class Model(FakeTransactionModel):
        found = []

        @classmethod
        def find(cls, *args, **kwargs):
            queries.append(args)
            return FakeQuery(cls.found)

    upload.monkeypatch.setattr(routes, "Transaction", Model)
    upload.monkeypatch.setattr(
        routes,
        "Parser",
        lambda rules: SimpleNamespace(
            parse=lambda contents: [
                SimpleNamespace(hash=f"h{n}", insert=lambda: _record([], "x"))
                for n in range(20)
            ]
        ),
    )

    assert client.post("/upload", files=csv_file()).json()["imported"] == 20
    assert len(queries) == 1
    assert queries[0][1] == {"hash": {"$in": [f"h{n}" for n in range(20)]}}


# --- Signing up ---------------------------------------------------------------


class FakeUser:
    """Stands in for the ``User`` document in the signup tests."""

    existing_username = None
    username = None

    def __init__(self, username, password_hash):
        self.username = username
        self.password_hash = password_hash
        self.id = "new-user-id"

    @classmethod
    async def find_one(cls, *args, **kwargs):
        if cls.existing_username is not None:
            return SimpleNamespace(username=cls.existing_username)
        return None

    async def insert(self):
        return self


def test_signup_rejects_mismatched_passwords(monkeypatch, public_client):
    """A typo in the confirmation field must not create an account."""
    monkeypatch.setattr(routes, "User", FakeUser)

    reply = public_client.post(
        "/signup",
        data={"username": "alice", "password": "a", "confirm_password": "b"},
    )

    assert reply.status_code == 400
    assert "did not match" in reply.text
    assert "session" not in public_client.cookies


def test_signup_rejects_a_taken_username(monkeypatch, public_client):
    """The friendlier error from routes.py, ahead of the database's own."""
    FakeUser.existing_username = "alice"
    monkeypatch.setattr(routes, "User", FakeUser)

    reply = public_client.post(
        "/signup",
        data={"username": "alice", "password": "a", "confirm_password": "a"},
    )

    assert reply.status_code == 400
    assert "already taken" in reply.text


def test_signup_creates_an_account_and_signs_it_in(monkeypatch, public_client):
    """A new username starts a session and lands on ``next``."""
    FakeUser.existing_username = None
    monkeypatch.setattr(routes, "User", FakeUser)

    reply = public_client.post(
        "/signup",
        data={
            "username": "alice",
            "password": "a",
            "confirm_password": "a",
            "next": "/patterns",
        },
    )

    assert reply.status_code == 303
    assert reply.headers["location"] == "/patterns"
    assert "session" in public_client.cookies
