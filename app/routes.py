"""
API route handlers for the FastAPI application.
"""

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import admin_user, current_user, hash_password, verify_password
from app.categories import CATEGORY_HIERARCHY as NEW_CATEGORY_HIERARCHY
from app.categorizer import Categorizer
from app.classification.client import ClassificationError
from app.classification.comparison import write_comparison
from app.classification.service import ClassificationService, apply, report_lines
from app.config import get_logger, settings
from app.constants import CATEGORY_HIERARCHY
from app.models import SOURCE_PENDING, CategoryRule, Transaction, User, object_id
from app.parser import Parser
from app.paypal import PayPalFormatError, import_export
from app.paypal.records import looks_like_export
from app.services import (
    TOP_DETAIL_TRANSACTIONS,
    CategorizationService,
    ChartDataService,
    ComdirectBalanceService,
    MerchantService,
    PatternService,
    RecurringService,
    ReviewService,
    SettingsService,
    TripService,
)

logger = get_logger(__name__)

#: Unauthenticated: the login form and the session it grants have to work
#: without a user to depend on.
public_router = APIRouter()
#: Every other route. The dependency is declared here rather than on each
#: handler so a route added later is protected by default; a handler that
#: also needs the user's id still declares its own Depends(current_user) —
#: FastAPI resolves a dependency once per request, so that costs nothing.
router = APIRouter(dependencies=[Depends(current_user)])
templates = Jinja2Templates(directory="app/templates")
CurrentUser = Annotated[User, Depends(current_user)]

# Exposed to every template: the frontend derives its category colour palette
# from this hierarchy instead of maintaining a parallel list of hex values.
templates.env.globals["CATEGORY_HIERARCHY"] = NEW_CATEGORY_HIERARCHY

#: Where the stylesheet and the page scripts live on disk.
STATIC_ROOT = Path("app/static")


def static_url(name: str) -> str:
    """
    A stylesheet or script URL that changes whenever the file does.

    Starlette's StaticFiles sends an ETag and a Last-Modified date but no
    Cache-Control, which leaves the browser free to apply its own heuristic —
    and Chrome's is to serve the file from memory for the rest of the session
    without revalidating. The effect is that an edit to a script silently does
    not reach the page: the HTML is always fresh because it is rendered per
    request, so the new markup arrives and the old code runs against it. A
    figure whose markup is server-rendered and whose data is drawn by script
    then comes up empty, which is exactly how this was found.

    Stamping the file's modification time into the query string sidesteps the
    heuristic entirely: a changed file is a different URL, and a URL the
    browser has never seen cannot be served from its cache.

    Args:
        name: File name inside ``app/static``.

    Returns:
        The URL with a version stamp, or the bare path if the file is missing
        — a missing asset is the server's problem to report, not something to
        fail template rendering over.
    """
    try:
        stamp = int((STATIC_ROOT / name).stat().st_mtime)
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={stamp}"


templates.env.globals["static_url"] = static_url
# The rule pages are hidden from the navigation while the engine is off.
templates.env.globals["RULES_ENABLED"] = settings.rules_enabled


def get_templates() -> Jinja2Templates:
    """Get Jinja2 templates instance."""
    return templates


async def nav_counts(user_id) -> dict[str, int]:
    """
    The badge counts the navigation bar draws next to Review and Categorize.

    They belong to the shared layout rather than to any one page, so every
    page route spreads this in. Passing them per route made the badges
    vanish on whichever page forgot to: an undefined count renders as
    nothing instead of failing, so the omission looked like an empty queue.
    """
    return {
        "review_count": await ReviewService.count(user_id),
        "uncategorized_count": await CategorizationService.get_uncategorized_count(
            user_id
        ),
    }


# ============================================================================
# Authentication
# ============================================================================


@public_router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: Annotated[str, Query()] = "/",
    mode: Annotated[str, Query()] = "signin",
) -> HTMLResponse:
    """The sign-in form, or the account-creation form next to it."""
    templates = get_templates()
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "next": next, "mode": mode, "error": None},
    )


@public_router.post("/login", response_class=HTMLResponse, response_model=None)
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> HTMLResponse | RedirectResponse:
    """Check a password and start a session."""
    user = await User.find_one(User.username == username)
    if user is None or not verify_password(password, user.password_hash):
        templates = get_templates()
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "next": next,
                "mode": "signin",
                "error": "Wrong username or password.",
            },
            status_code=401,
        )
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url=next or "/", status_code=303)


@public_router.post("/signup", response_class=HTMLResponse, response_model=None)
async def signup(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm_password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> HTMLResponse | RedirectResponse:
    """Create an account and start a session with it."""
    templates = get_templates()

    def rejected(error: str) -> HTMLResponse:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "next": next, "mode": "signup", "error": error},
            status_code=400,
        )

    if password != confirm_password:
        return rejected("Passwords did not match.")
    if await User.find_one(User.username == username) is not None:
        return rejected(f"Username {username!r} is already taken.")

    user = await User(username=username, password_hash=hash_password(password)).insert()
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url=next or "/", status_code=303)


@public_router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """End the session."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# ============================================================================
# Page Routes
# ============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: CurrentUser) -> HTMLResponse:
    """Render the dashboard page."""
    templates = get_templates()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Dashboard",
            "active_path": "/",
            "balances_enabled": user.balances_enabled,
            **await nav_counts(user.id),
        },
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """Render the CSV upload page."""
    templates = get_templates()
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "title": "Upload",
            "active_path": "/upload",
            **await nav_counts(user.id),
        },
    )


@router.get("/categorize", response_class=HTMLResponse)
async def categorize_page(
    request: Request,
    user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """
    Render the categorization page with an uncategorized transaction.

    Kept working after the rule engine was switched off, because switching it
    back on must not require restoring a page first. It writes rules that only
    take effect while ``rules_enabled`` is true.

    Query Parameters:
        skip: Offset into the review queue, used by the Skip action to move on
            without writing a rule.
    """
    templates = get_templates()
    transaction = await CategorizationService.get_uncategorized_transaction(
        user.id, skip=skip
    )

    return templates.TemplateResponse(
        "categorize.html",
        {
            "request": request,
            "title": "Categorize",
            "active_path": "/categorize",
            **await nav_counts(user.id),
            "transaction": transaction,
            "categories": CATEGORY_HIERARCHY,
            "skip": skip,
        },
    )


@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    user: CurrentUser,
    skip: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """
    Transactions the classifier would not decide, least confident first.

    Correcting one here settles that transaction and teaches the merchant
    cache, so every future transaction from the same merchant is filed the
    same way without an API call.
    """
    templates = get_templates()
    transactions = await ReviewService.queue(user.id, limit=50, skip=skip)
    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "title": "Needs Review",
            "active_path": "/review",
            **await nav_counts(user.id),
            "transactions": transactions,
            "paypal": await ReviewService.paypal_context(user.id, transactions),
            "categories": NEW_CATEGORY_HIERARCHY,
            "skip": skip,
        },
    )


@router.post("/review/{transaction_id}")
async def correct_category(
    transaction_id: str,
    user: CurrentUser,
    major_category: str = Form(...),
    minor_category: str = Form(...),
) -> RedirectResponse:
    """Assign a category by hand and record it for the merchant."""
    try:
        await ReviewService.correct(
            user.id, transaction_id, major_category, minor_category
        )
    except ValueError as error:
        # Covers both an unknown transaction and a category pair that does not
        # exist; either way there is nothing to save.
        raise HTTPException(status_code=422, detail=str(error))
    return RedirectResponse(url="/review", status_code=303)


@router.get("/recurring", response_class=HTMLResponse)
async def recurring_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """
    Payments that repeat on a schedule.

    Replaces the old Subscriptions category: whether a payment recurs is a
    property of the history, not of what the merchant sells.
    """
    templates = get_templates()
    return templates.TemplateResponse(
        "recurring.html",
        {
            "request": request,
            "title": "Recurring",
            "active_path": "/recurring",
            **await nav_counts(user.id),
            "overview": await RecurringService.overview(user.id),
        },
    )


@router.post("/api/recurring/detect")
async def detect_recurring(user: CurrentUser) -> dict:
    """Re-run detection over the whole history and store the result."""
    runs = await RecurringService.detect_and_store(user.id)
    return {
        "status": "success",
        "schedules": len(runs),
        "transactions": sum(run.occurrences for run in runs),
    }


@router.post("/api/recurring/override")
async def set_recurring_override(
    user: CurrentUser,
    schedule_id: Annotated[str, Form()],
    decision: Annotated[str, Form()],
) -> dict:
    """
    Record what the user says about one detected schedule.

    Detection reads a pattern out of dates and amounts, so it can only guess
    at the two things this settles: whether the pattern is a schedule at all,
    and whether it is still running. Neither can be fixed by tuning a
    threshold without breaking the other case, so the user has the last word.

    Form fields:
        schedule_id: The id the recurring page carries for a detected run.
        decision: ``dismissed``, ``active`` or ``ended``.
    """
    try:
        stored = await RecurringService.set_override(user.id, schedule_id, decision)
    except ValueError as error:
        # Covers an unknown decision and a schedule that detection no longer
        # produces; neither is something to store.
        raise HTTPException(status_code=422, detail=str(error))
    return {"status": "success", **stored}


@router.post("/api/recurring/override/clear")
async def clear_recurring_override(
    user: CurrentUser,
    schedule_id: Annotated[str, Form()],
) -> dict:
    """Hand one schedule back to the detector."""
    try:
        cleared = await RecurringService.clear_override(user.id, schedule_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"status": "success", "cleared": cleared}


# ============================================================================
# Page Routes - Patterns
# ============================================================================


@router.get("/patterns", response_class=HTMLResponse)
async def patterns_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """
    What changed in the spending, and what reliably does not.

    The dashboard draws every month next to every other; this page is the part
    that says which of those differences mean something. Everything on it is
    recomputed from the history on each load, so nothing here is stored except
    the findings the user has put away.
    """
    templates = get_templates()
    return templates.TemplateResponse(
        "patterns.html",
        {
            "request": request,
            "title": "Patterns",
            "active_path": "/patterns",
            **await nav_counts(user.id),
            "overview": await PatternService.overview(user.id),
        },
    )


@router.post("/api/patterns/dismiss")
async def dismiss_pattern(
    user: CurrentUser,
    kind: Annotated[str, Form()],
    subject: Annotated[str, Form()] = "",
    month: Annotated[str, Form()] = "",
) -> dict:
    """
    Put one finding away.

    Findings are recomputed on every load, so the dismissal is keyed on what
    the finding *is* — its kind, its subject and the month it concerns —
    rather than on any handle the page carries. The same category drifting
    again next year is a different month, and is offered again.

    Form fields:
        kind: Which question produced it, e.g. ``drift`` or ``outlier``.
        subject: The category or merchant it is about; empty for findings
            about the ledger as a whole.
        month: The month it concerns; empty where it concerns no single one.
    """
    try:
        stored = await PatternService.dismiss(user.id, kind, subject, month)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"status": "success", "stored": stored}


@router.post("/api/patterns/restore")
async def restore_pattern(
    user: CurrentUser,
    kind: Annotated[str, Form()],
    subject: Annotated[str, Form()] = "",
    month: Annotated[str, Form()] = "",
) -> dict:
    """Hand one dismissed finding back to the page."""
    return {
        "status": "success",
        "restored": await PatternService.restore(user.id, kind, subject, month),
    }


@router.get("/merchants", response_class=HTMLResponse)
async def merchants_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """
    Who actually gets the money, and how few of them there are.

    The dashboard aggregates by category and stops there. This page opens a
    category up: merchants ranked by what they were paid over the last
    complete year, how much of the spending the top of that list holds, and
    which categories are one merchant wearing a category's name. Nothing on it
    is stored — concentration is a property of the history rather than a
    proposal about it, so there is no decision to record.
    """
    templates = get_templates()
    return templates.TemplateResponse(
        "merchants.html",
        {
            "request": request,
            "title": "Merchants",
            "active_path": "/merchants",
            **await nav_counts(user.id),
            "overview": await MerchantService.overview(user.id),
        },
    )


# ============================================================================
# Page Routes - Trips
# ============================================================================


def _trip_day(value: str, field: str) -> datetime:
    """
    One date from a form, as the datetime the trip window is measured in.

    Raises:
        HTTPException: The browser sent something that is not a date. The date
            inputs make that unlikely, but a form post is a public surface.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail=f"{field} is not a date")


def _trip_budget(value: str | None) -> float | None:
    """A budget from a form, or None where the field was left empty."""
    if value is None or not str(value).strip():
        return None
    try:
        return abs(float(str(value).replace(",", ".")))
    except ValueError:
        raise HTTPException(status_code=422, detail="Budget is not an amount")


@router.get("/trips", response_class=HTMLResponse)
async def trips_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """
    Every trip, and what it cost.

    A trip's spending is not contained in its dates — flights are bought
    months ahead and card payments abroad settle after the flight home — so
    what is totalled here is what was assigned by hand, never a date filter.
    """
    templates = get_templates()
    return templates.TemplateResponse(
        "trips.html",
        {
            "request": request,
            "title": "Trips",
            "active_path": "/trips",
            **await nav_counts(user.id),
            "overview": await TripService.overview(user.id),
            # Detection runs on every load rather than on a button: a
            # proposal is worthless once acted on, and the alternative is a
            # stale list that has to be refreshed by hand.
            "found": await TripService.proposals(user.id),
        },
    )


@router.post("/trips")
async def create_trip(
    user: CurrentUser,
    name: Annotated[str, Form()],
    start_date: Annotated[str, Form()],
    end_date: Annotated[str, Form()],
    destination: Annotated[str, Form()] = "",
    budget: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Create a trip and go straight to it, where the review queue is."""
    try:
        trip = await TripService.create(
            user.id,
            name=name,
            start_date=_trip_day(start_date, "Start date"),
            end_date=_trip_day(end_date, "End date"),
            destination=destination,
            budget=_trip_budget(budget),
            notes=notes,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return RedirectResponse(url=f"/trips/{trip.id}", status_code=303)


@router.post("/api/trips/proposals/{anchor_id}/dismiss")
async def dismiss_trip_proposal(anchor_id: str, user: CurrentUser) -> dict:
    """
    Refuse one detected trip, so it stops being proposed.

    Detection is recomputed on every load, so without this the same wrongly
    flagged week of online shopping would come back after every upload.
    """
    try:
        dismissed = await TripService.dismiss_proposal(user.id, anchor_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"status": "success", "dismissed": dismissed}


@router.post("/api/trips/proposals/{anchor_id}/restore")
async def restore_trip_proposal(anchor_id: str, user: CurrentUser) -> dict:
    """Hand one refused proposal back to the detector."""
    restored = await TripService.restore_proposal(user.id, anchor_id)
    return {"status": "success", "restored": restored}


@router.get("/trips/{trip_id}", response_class=HTMLResponse)
async def trip_page(request: Request, trip_id: str, user: CurrentUser) -> HTMLResponse:
    """
    One trip: what it cost, what is assigned to it, and what it may be missing.

    The queue at the foot of the page is the whole point. Detection can only
    propose from three windows around the dates — the booking lead-in, the
    trip itself, the settlement tail — and which of those proposals is
    actually the holiday is a question only the traveller can answer.
    """
    templates = get_templates()
    try:
        trip = await TripService.detail(user.id, trip_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Trip not found")
    return templates.TemplateResponse(
        "trip.html",
        {
            "request": request,
            "title": trip["name"],
            "active_path": "/trips",
            **await nav_counts(user.id),
            "trip": trip,
        },
    )


@router.post("/trips/{trip_id}/edit")
async def edit_trip(
    trip_id: str,
    user: CurrentUser,
    name: Annotated[str, Form()],
    start_date: Annotated[str, Form()],
    end_date: Annotated[str, Form()],
    destination: Annotated[str, Form()] = "",
    budget: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """
    Rewrite a trip.

    Moving the dates re-runs the suggester and touches no assignment: a date
    correction is not a retraction of a decision the user already made.
    """
    try:
        await TripService.update(
            user.id,
            trip_id,
            name=name,
            start_date=_trip_day(start_date, "Start date"),
            end_date=_trip_day(end_date, "End date"),
            destination=destination,
            budget=_trip_budget(budget),
            notes=notes,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return RedirectResponse(url=f"/trips/{trip_id}", status_code=303)


@router.post("/trips/{trip_id}/delete")
async def delete_trip(trip_id: str, user: CurrentUser) -> RedirectResponse:
    """Remove a trip and every decision made about it."""
    try:
        await TripService.delete(user.id, trip_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Trip not found")
    return RedirectResponse(url="/trips", status_code=303)


# ============================================================================
# API Routes - Trips
# ============================================================================


@router.post("/api/trips/{trip_id}/assign")
async def assign_to_trip(
    trip_id: str,
    user: CurrentUser,
    transaction_id: Annotated[str, Form()],
    decision: Annotated[str, Form()] = "",
    share: Annotated[float, Form()] = 1.0,
) -> dict:
    """
    Say whether one transaction belongs to this trip.

    Form fields:
        transaction_id: The transaction being decided on.
        decision: ``included``, ``excluded``, or empty to undo the decision
            and hand the transaction back to the suggester.
        share: The fraction of the amount this trip owns, for a booking that
            covers people who are not on its books.

    Returns:
        The trip's recomputed totals, so the page can update in place instead
        of reloading after every decision in a queue.
    """
    try:
        if decision:
            result = await TripService.assign(
                user.id, trip_id, transaction_id, decision, share=share
            )
        else:
            result = await TripService.clear(user.id, trip_id, transaction_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"status": "success", **result}


@router.post("/api/trips/{trip_id}/split")
async def split_trip(
    trip_id: str,
    user: CurrentUser,
    ways: Annotated[int, Form()],
) -> dict:
    """
    Say how many people the whole journey was shared between.

    One number for the trip, not a rewrite of every assignment: splitting a
    trip is a fact about the trip, and setting it payment by payment would
    overwrite the shares set by hand with no way back. Send ``ways=1`` to put
    the whole total back.

    Form fields:
        ways: How many people shared the trip.

    Returns:
        The trip's recomputed totals.
    """
    try:
        result = await TripService.set_split(user.id, trip_id, ways)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"status": "success", **result}


@router.post("/api/trips/{trip_id}/assign-selected")
async def assign_selected_to_trip(
    trip_id: str,
    user: CurrentUser,
    transaction_id: Annotated[list[str], Form()],
) -> dict:
    """
    Assign the transactions the user has selected in the queue.

    Takes the ids explicitly rather than re-deriving the detector's own
    suggestions, so that what the page shows selected and what is written are
    the same set.

    Form fields:
        transaction_id: Repeated, one per selected transaction.
    """
    try:
        written = await TripService.assign_many(user.id, trip_id, transaction_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))
    return {"status": "success", "assigned": written}


@router.get("/api/trips/{trip_id}/search")
async def search_trip_transactions(
    trip_id: str,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=2)],
) -> dict:
    """
    Find a transaction to attach by hand.

    The suggester's windows are deliberately narrow, so there has to be a way
    to reach the line they never offered — a deposit paid two years out, a
    friend settling up long after the settlement tail closed.
    """
    try:
        results = await TripService.search(user.id, trip_id, q)
    except ValueError:
        raise HTTPException(status_code=404, detail="Trip not found")
    return {"status": "success", "query": q, "results": results}


@router.get("/rules", response_class=HTMLResponse)
async def rules_page(
    request: Request,
    user: CurrentUser,
    search: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render the rules management page with search and category grouping."""
    templates = get_templates()
    rules = (
        await CategoryRule.find(CategoryRule.user_id == user.id)
        .sort("major", "minor", "substring")
        .to_list()
    )

    search_value = (search or "").strip().lower()
    if search_value:
        rules = [
            rule
            for rule in rules
            if search_value in rule.substring.lower()
            or search_value in rule.major.lower()
            or search_value in rule.minor.lower()
            or (rule.process and search_value in rule.process.lower())
        ]

    grouped_rules = {}
    for rule in rules:
        grouped_rules.setdefault(rule.major, []).append(rule)

    return templates.TemplateResponse(
        "rules.html",
        {
            "request": request,
            "title": "Rules",
            "active_path": "/rules",
            **await nav_counts(user.id),
            "rules": rules,
            "grouped_rules": grouped_rules,
            "search": search or "",
            "categories": CATEGORY_HIERARCHY,
        },
    )


@router.post("/rules/{rule_id}/delete", response_class=RedirectResponse)
async def delete_rule(rule_id: str, user: CurrentUser) -> RedirectResponse:
    """Delete a categorization rule by id and recompute transaction categories."""
    try:
        rule = await CategoryRule.get(object_id(rule_id))
    except ValueError:
        rule = None
    if not rule or rule.user_id != user.id:
        raise HTTPException(status_code=404, detail="Rule not found")

    await rule.delete()
    logger.info(f"Deleted rule: {rule_id}")

    remaining_rules = await CategoryRule.find(CategoryRule.user_id == user.id).to_list()
    categorizer = Categorizer(rules=remaining_rules)
    await CategorizationService.recompute_all_categories(user.id, categorizer)

    return RedirectResponse(url="/rules", status_code=303)


# ============================================================================
# Settings
# ============================================================================


async def _settings_context(request: Request, user: User) -> dict:
    """The context every /settings render needs, success or refusal alike."""
    is_admin = await SettingsService.is_admin(user)
    status = await ComdirectBalanceService.status(user.id)
    credentials = (
        await ComdirectBalanceService.credentials(user.id)
        if status["has_credentials"]
        else None
    )
    return {
        "request": request,
        "title": "Settings",
        "active_path": "/settings",
        **await nav_counts(user.id),
        "is_admin": is_admin,
        "anthropic_state": (
            await SettingsService.anthropic_state() if is_admin else None
        ),
        "anthropic_key": (await SettingsService.anthropic_key() if is_admin else None),
        "has_credentials": status["has_credentials"],
        "credentials": credentials,
        "balances_enabled": user.balances_enabled,
    }


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, user: CurrentUser) -> HTMLResponse:
    """Render the settings page."""
    templates = get_templates()
    return templates.TemplateResponse(
        "settings.html", await _settings_context(request, user)
    )


async def _settings_refused(request: Request, user: User, error: str) -> HTMLResponse:
    templates = get_templates()
    context = await _settings_context(request, user)
    context["error"] = error
    return templates.TemplateResponse("settings.html", context, status_code=422)


@router.post("/settings/balances", response_class=HTMLResponse, response_model=None)
async def save_settings_balances(
    request: Request,
    user: CurrentUser,
    enabled: Annotated[bool, Form()] = False,
    client_id: Annotated[str, Form()] = "",
    client_secret: Annotated[str, Form()] = "",
    access_number: Annotated[str, Form()] = "",
    pin: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """
    Save a Comdirect login, if one was entered, then apply the toggle.

    The two are one form and one button: credentials are only worth entering
    to turn the snapshot on, so saving them and enabling it happen together.
    Blank fields leave an existing login as it is, letting the toggle move on
    its own once a login is already stored.
    """
    fields = [client_id, client_secret, access_number, pin]
    try:
        if any(fields):
            if not all(fields):
                raise ValueError("Fill in all four Comdirect fields, or none.")
            await ComdirectBalanceService.save_credentials(
                user.id, client_id, client_secret, access_number, pin
            )
        await SettingsService.set_balances_enabled(user, enabled)
    except ValueError as error:
        return await _settings_refused(request, user, str(error))
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/balances/revoke")
async def revoke_settings_balances(user: CurrentUser) -> RedirectResponse:
    """Delete this user's Comdirect login and switch the snapshot off."""
    await SettingsService.revoke_comdirect(user)
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/anthropic", response_class=HTMLResponse, response_model=None)
async def save_settings_anthropic(
    request: Request,
    user: Annotated[User, Depends(admin_user)],
    key: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    """Save the app-wide Anthropic API key."""
    try:
        await SettingsService.save_anthropic_key(key)
    except ValueError as error:
        return await _settings_refused(request, user, str(error))
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/settings/anthropic/revoke")
async def revoke_settings_anthropic(
    user: Annotated[User, Depends(admin_user)],
) -> RedirectResponse:
    """Delete the stored Anthropic key and fall back to .env."""
    await SettingsService.revoke_anthropic_key()
    return RedirectResponse(url="/settings", status_code=303)


# ============================================================================
# API Routes - Analytics
# ============================================================================


@router.get("/api/chart-data")
async def get_chart_data(
    user: CurrentUser,
    year: Annotated[int | None, Query()] = None,
    categories: Annotated[list[str], Query()] = None,
    category_type: Annotated[str | None, Query()] = None,
) -> dict:
    """
    Get chart data aggregated by month and category.

    Query Parameters:
        year: Filter by year (optional)
        categories: List of minor categories to include (optional)

    Returns:
        JSON with months, categories, data structure, and available years
    """
    return await ChartDataService.get_chart_data(
        user.id,
        year=year,
        categories=categories or [],
        category_type=category_type,
    )


@router.get("/api/cashflow-data")
async def get_cashflow_data(
    user: CurrentUser,
    year: Annotated[int | None, Query()] = None,
) -> dict:
    """
    Get net cashflow data aggregated by month.
    Excludes Uncategorized and Internal transfers.

    Query Parameters:
        year: Filter by year (optional)

    Returns:
        JSON with months, data values, total, and available years
    """
    return await ChartDataService.get_cashflow_data(user.id, year=year)


@router.get("/api/transaction-detail")
async def get_transaction_detail(
    user: CurrentUser,
    month: Annotated[str | None, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")] = None,
    year: Annotated[int | None, Query()] = None,
    categories: Annotated[list[str] | None, Query()] = None,
    category_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = TOP_DETAIL_TRANSACTIONS,
) -> dict:
    """
    Get the transactions behind one thing the dashboard drew.

    Backs the drill-down popup every plot opens. A click always resolves to a
    period and optionally some categories, so one endpoint answers for all of
    them. Both directions come back separately, because what was clicked is a
    net sum and a refund filed under an expense category belongs to it just as
    much as the spending does.

    Categories are repeatable because not every slice stands for one of them:
    a breakdown's "N smaller categories" wedge names the whole tail it
    collapsed, and opens just like any other part of the chart.

    Query Parameters:
        month: Month key in YYYY-MM format (optional)
        year: Year to cover (optional; ignored when month is given, and
            omitting both covers all of history)
        categories: Categories to narrow to, repeatable (optional)
        category_type: Whether categories names major or minor categories.
            Must match the granularity the plot is drawn at.
        limit: How many transactions to return per direction

    Returns:
        JSON echoing the scope, with its net total and an incoming and
        outgoing side
    """
    return await ChartDataService.get_transaction_detail(
        user.id,
        month=month,
        year=year,
        categories=categories,
        category_type=category_type,
        limit=limit,
    )


@router.get("/api/comdirect/status")
async def get_comdirect_status(user: CurrentUser) -> dict:
    """State the balances panel renders from: credentials, pending, data."""
    return await ComdirectBalanceService.status(user.id)


@router.post("/api/comdirect/session")
async def start_comdirect_session(user: CurrentUser) -> dict:
    """Start a Comdirect login: sends the photoTAN push."""
    try:
        return await ComdirectBalanceService.begin_session(user.id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


@router.post("/api/comdirect/session/confirm")
async def confirm_comdirect_session(user: CurrentUser) -> dict:
    """Finish a Comdirect login after the photoTAN was confirmed in-app."""
    try:
        return await ComdirectBalanceService.confirm_session(user.id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


# ============================================================================
# API Routes - Classification
# ============================================================================


@router.post("/api/classify")
async def classify_transactions(
    user: CurrentUser,
    transaction_ids: list[str] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 25,
    persist: Annotated[bool, Query()] = False,
    use_cache: Annotated[bool, Query()] = True,
) -> dict:
    """
    Classify transactions through the Claude API.

    Consults the merchant cache first and only sends unknown merchants on, so
    a run over already-known transactions costs nothing.

    Body:
        transaction_ids: Specific transactions, as a JSON list. A bare list
            parameter is read from the body rather than the query string.
            Defaults to the ones still awaiting classification.

    Query Parameters:
        limit: How many to take when no ids are given.
        persist: Write the results back. Off by default, so a run can be
            inspected before it changes anything.
        use_cache: Set to false to send every transaction to the API.

    Returns:
        The classification of each transaction and a summary of the run.
    """
    if transaction_ids:
        # An id that is not an id names no transaction, which is the same
        # answer as an id naming one that is gone: it is left out, and the
        # caller sees it missing from the results.
        transactions = []
        for tid in transaction_ids:
            try:
                transaction = await Transaction.get(object_id(tid))
            except ValueError:
                continue
            if transaction is not None and transaction.user_id == user.id:
                transactions.append(transaction)
    else:
        transactions = await (
            Transaction.find(
                Transaction.user_id == user.id,
                Transaction.classification_source == SOURCE_PENDING,
            )
            .sort("-date")
            .limit(limit)
            .to_list()
        )

    if not transactions:
        return {"status": "empty", "classified": 0, "results": []}

    try:
        run = await ClassificationService().classify(
            user.id, transactions, use_cache=use_cache
        )
    except ClassificationError as error:
        raise HTTPException(status_code=503, detail=str(error))

    written = await apply(run.classifications, transactions) if persist else 0

    return {
        "status": "success",
        "classified": len(run.classifications),
        "persisted": written,
        "summary": {
            "cache_hits": run.cache_hits,
            "api_calls": run.api_calls,
            "fallbacks": run.fallbacks,
            "below_threshold": run.below_threshold,
            "invalid_pairs": run.invalid_pairs,
            "failed": run.failed,
        },
        "usage": run.usage,
        "results": [
            {
                "transaction_id": c.transaction_id,
                "merchant": c.merchant,
                "major": c.major,
                "minor": c.minor,
                "flow_type": c.flow_type,
                "confidence": round(c.confidence, 3),
                "reason": c.reason,
                "source": c.source,
                "needs_review": c.needs_review,
            }
            for c in run.classifications
        ],
    }


# ============================================================================
# API Routes - CSV Upload
# ============================================================================


@router.post("/upload")
async def upload_csv(user: CurrentUser, files: list[UploadFile] = File(...)) -> dict:
    """
    Upload and process CSV files.

    Args:
        files: List of CSV files to upload

    Returns:
        Status, imported count, and file results

    Raises:
        HTTPException: If no files provided or invalid file type
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one CSV file is required")

    for file in files:
        if not file.filename or not file.filename.lower().endswith(".csv"):
            raise HTTPException(
                status_code=400,
                detail=f"Only CSV files allowed (invalid: {file.filename})",
            )

    # Load existing rules
    rules = await CategoryRule.find(CategoryRule.user_id == user.id).to_list()
    parser = Parser(rules=rules)

    imported_total = 0
    file_results = []
    imported_new: list[Transaction] = []
    paypal_rows = 0

    for file in files:
        try:
            contents = await file.read()

            # A PayPal activity export is not a statement and holds no
            # transactions of its own. It is evidence about statements: the
            # payees Comdirect truncated out of its PayPal lines. Storing it
            # here means one upload control for both, and the classifier picks
            # the payees up on the next import. See app.paypal.
            if looks_like_export(contents):
                result = await import_export(user.id, contents)
                paypal_rows += result.added
                file_results.append(
                    {
                        "filename": file.filename,
                        "kind": "paypal",
                        "imported": result.added,
                        "skipped": result.already_known,
                    }
                )
                logger.info(
                    "Processed %s (PayPal): %s", file.filename, result.summary()
                )
                continue

            transactions = parser.parse(contents)

            # Which of this file's transactions are already stored, in one
            # query rather than one per row. Monthly exports overlap by
            # design, so most of a file is usually already known and asking
            # about each row separately made the round trips, not the work,
            # the cost of an import.
            known = {
                row.hash
                for row in await Transaction.find(
                    Transaction.user_id == user.id,
                    {"hash": {"$in": [t.hash for t in transactions]}},
                ).to_list()
            }

            # Inserted one at a time on purpose: insert_many returns the ids
            # in a result object instead of writing them onto the documents,
            # and everything downstream of here keys on transaction.id.
            file_imported = 0
            file_skipped = 0
            for transaction in transactions:
                if transaction.hash in known:
                    file_skipped += 1
                    continue
                known.add(transaction.hash)
                transaction.user_id = user.id
                await transaction.insert()
                imported_new.append(transaction)
                file_imported += 1

            imported_total += file_imported
            file_results.append(
                {
                    "filename": file.filename,
                    "imported": file_imported,
                    "skipped": file_skipped,
                }
            )
            logger.info(
                f"Processed {file.filename}: {file_imported} imported, "
                f"{file_skipped} skipped"
            )

        except PayPalFormatError as e:
            logger.error(f"Error reading PayPal export {file.filename}: {e}")
            raise HTTPException(
                status_code=422,
                detail=f"PayPal export unreadable in {file.filename}: {e}",
            )
        except Exception as e:
            logger.error(f"Error parsing {file.filename}: {str(e)}")
            raise HTTPException(
                status_code=422,
                detail=f"Parsing error in {file.filename}: {str(e)}",
            )

    result: dict = {
        "status": "success",
        "imported": imported_total,
        "files": file_results,
    }
    if paypal_rows:
        result["paypal_rows"] = paypal_rows

    if imported_new:
        result.update(await _classify_import(user.id, imported_new))

    return result


async def _classify_import(user_id, transactions: list[Transaction]) -> dict:
    """
    Run the classifier over freshly imported transactions.

    The rule engine has already had its say during parsing, and its verdict is
    still on the transaction when this runs. Both are written to the
    comparison log before anything is overwritten, which is the only way to
    see where they differ on real imports — and therefore the only basis for
    deciding the rule engine can go.

    A classification failure must not fail the import: the transactions are
    already stored, they simply keep whatever the rules said.
    """
    summary: dict = {}
    try:
        run = await ClassificationService().classify(user_id, transactions)
    except ClassificationError as error:
        logger.warning("Import not classified: %s", error)
        return {"classification": {"status": "unavailable", "detail": str(error)}}
    except Exception as error:  # noqa: BLE001 — the import itself must survive
        logger.error("Classification failed during import: %s", error)
        return {"classification": {"status": "failed", "detail": str(error)}}

    # The comparison needs both verdicts. With the rule engine off there is
    # only one, and a log of the classifier agreeing with itself is noise.
    if settings.comparison_mode_enabled and settings.rules_enabled:
        path, agreed, differed = write_comparison(transactions, run.classifications)
        summary["comparison"] = {
            "log": str(path),
            "agreed": agreed,
            "differed": differed,
        }

    if settings.classification_enabled:
        summary["persisted"] = await apply(run.classifications, transactions)
    else:
        # The rule engine stays in charge; the log still records both.
        summary["persisted"] = 0

    summary["report"] = report_lines(run, len(transactions))
    return {"classification": summary}


# ============================================================================
# API Routes - Categorization
# ============================================================================


@router.post("/categorize")
async def save_categorize_rule(
    user: CurrentUser,
    transaction_id: str = Form(...),
    substring: str = Form(...),
    major_category: str = Form(...),
    minor_category: str = Form(...),
    process: str | None = Form(None),
    use_process: str | None = Form(None),
) -> RedirectResponse:
    """
    Save a categorization rule and recompute categories.

    Args:
        transaction_id: ID of the transaction being categorized
        substring: Substring pattern to match
        major_category: Major category to assign
        minor_category: Minor category to assign
        process: Process code to match (optional)
        use_process: Whether to use transaction's process code

    Returns:
        Redirect to categorize page

    Raises:
        HTTPException: If transaction not found or invalid substring
    """
    # Fetch and validate transaction
    try:
        transaction = await Transaction.get(object_id(transaction_id))
    except ValueError:
        transaction = None
    if not transaction or transaction.user_id != user.id:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # Process substring input
    substring_value = substring.strip()
    if not substring_value:
        raise HTTPException(status_code=422, detail="Substring is required")

    # If substring is numeric, take first N words from booking text
    if substring_value.isnumeric():
        substring_value = " ".join(
            transaction.booking_text.split()[: int(substring_value)]
        )
    elif substring_value.upper() not in transaction.booking_text.upper():
        raise HTTPException(
            status_code=422,
            detail="Substring must be part of the booking text (case-insensitive)",
        )

    # Process process code input
    process_value = process.strip() if process else None
    if not process_value and use_process and transaction.process:
        process_value = transaction.process

    # Create and save new rule
    new_rule = CategoryRule(
        user_id=user.id,
        substring=substring_value,
        process=process_value,
        major=major_category,
        minor=minor_category,
    )
    await new_rule.insert()
    logger.info(
        f"Created rule: substring='{substring_value}', "
        f"major={major_category}, minor={minor_category}"
    )

    # Recompute categories for transactions that are still uncategorized
    rules = await CategoryRule.find(CategoryRule.user_id == user.id).to_list()
    categorizer = Categorizer(rules=rules)
    await CategorizationService.recompute_all_categories(
        user.id,
        categorizer,
        only_uncategorized=True,
    )

    return RedirectResponse(url="/categorize", status_code=303)
