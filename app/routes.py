"""
API route handlers for the FastAPI application.
"""

from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.categories import CATEGORY_HIERARCHY as NEW_CATEGORY_HIERARCHY
from app.categorizer import Categorizer
from app.classification.client import ClassificationError
from app.classification.comparison import write_comparison
from app.classification.service import ClassificationService, apply, report_lines
from app.config import get_logger, settings
from app.constants import CATEGORY_HIERARCHY
from app.models import SOURCE_PENDING, CategoryRule, Transaction
from app.parser import Parser
from app.services import (
    CategorizationService,
    ChartDataService,
    ComdirectBalanceService,
    RecurringService,
    ReviewService,
)

logger = get_logger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Exposed to every template: the frontend derives its category colour palette
# from this hierarchy instead of maintaining a parallel list of hex values.
templates.env.globals["CATEGORY_HIERARCHY"] = NEW_CATEGORY_HIERARCHY
# The rule pages are hidden from the navigation while the engine is off.
templates.env.globals["RULES_ENABLED"] = settings.rules_enabled


def get_templates() -> Jinja2Templates:
    """Get Jinja2 templates instance."""
    return templates


# ============================================================================
# Page Routes
# ============================================================================


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the dashboard page."""
    templates = get_templates()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "title": "Dashboard",
            "active_path": "/",
            "uncategorized_count": (
                await CategorizationService.get_uncategorized_count()
            ),
            "review_count": await ReviewService.count(),
            "render_balance_section": (
                ComdirectBalanceService.should_render_balance_section()
            ),
        },
    )


@router.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request) -> HTMLResponse:
    """Render the CSV upload page."""
    templates = get_templates()
    return templates.TemplateResponse(
        "upload.html",
        {
            "request": request,
            "title": "Upload",
            "active_path": "/upload",
        },
    )


@router.get("/categorize", response_class=HTMLResponse)
async def categorize_page(
    request: Request,
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
    transaction = await CategorizationService.get_uncategorized_transaction(skip=skip)
    uncategorized_count = await CategorizationService.get_uncategorized_count()

    return templates.TemplateResponse(
        "categorize.html",
        {
            "request": request,
            "title": "Categorize",
            "active_path": "/categorize",
            "transaction": transaction,
            "categories": CATEGORY_HIERARCHY,
            "uncategorized_count": uncategorized_count,
            "skip": skip,
        },
    )


@router.get("/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    skip: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """
    Transactions the classifier would not decide, least confident first.

    Correcting one here settles that transaction and teaches the merchant
    cache, so every future transaction from the same merchant is filed the
    same way without an API call.
    """
    templates = get_templates()
    transactions = await ReviewService.queue(limit=50, skip=skip)
    return templates.TemplateResponse(
        "review.html",
        {
            "request": request,
            "title": "Needs Review",
            "active_path": "/review",
            "transactions": transactions,
            "review_count": await ReviewService.count(),
            "categories": NEW_CATEGORY_HIERARCHY,
            "skip": skip,
            "uncategorized_count": (
                await CategorizationService.get_uncategorized_count()
            ),
        },
    )


@router.post("/review/{transaction_id}")
async def correct_category(
    transaction_id: str,
    major_category: str = Form(...),
    minor_category: str = Form(...),
) -> RedirectResponse:
    """Assign a category by hand and record it for the merchant."""
    try:
        await ReviewService.correct(transaction_id, major_category, minor_category)
    except ValueError as error:
        # Covers both an unknown transaction and a category pair that does not
        # exist; either way there is nothing to save.
        raise HTTPException(status_code=422, detail=str(error))
    return RedirectResponse(url="/review", status_code=303)


@router.get("/recurring", response_class=HTMLResponse)
async def recurring_page(request: Request) -> HTMLResponse:
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
            "overview": await RecurringService.overview(),
            "uncategorized_count": (
                await CategorizationService.get_uncategorized_count()
            ),
        },
    )


@router.post("/api/recurring/detect")
async def detect_recurring() -> dict:
    """Re-run detection over the whole history and store the result."""
    runs = await RecurringService.detect_and_store()
    return {
        "status": "success",
        "schedules": len(runs),
        "transactions": sum(run.occurrences for run in runs),
    }


@router.get("/rules", response_class=HTMLResponse)
async def rules_page(
    request: Request,
    search: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """Render the rules management page with search and category grouping."""
    templates = get_templates()
    rules = await CategoryRule.find_all().sort("major", "minor", "substring").to_list()

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
            "rules": rules,
            "grouped_rules": grouped_rules,
            "search": search or "",
            "categories": CATEGORY_HIERARCHY,
        },
    )


@router.post("/rules/{rule_id}/delete", response_class=RedirectResponse)
async def delete_rule(rule_id: str) -> RedirectResponse:
    """Delete a categorization rule by id and recompute transaction categories."""
    rule = await CategoryRule.get(PydanticObjectId(rule_id))
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found")

    await rule.delete()
    logger.info(f"Deleted rule: {rule_id}")

    remaining_rules = await CategoryRule.find_all().to_list()
    categorizer = Categorizer(rules=remaining_rules)
    await CategorizationService.recompute_all_categories(categorizer)

    return RedirectResponse(url="/rules", status_code=303)


# ============================================================================
# API Routes - Analytics
# ============================================================================


@router.get("/api/chart-data")
async def get_chart_data(
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
        year=year,
        categories=categories or [],
        category_type=category_type,
    )


@router.get("/api/cashflow-data")
async def get_cashflow_data(
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
    return await ChartDataService.get_cashflow_data(year=year)


@router.get("/api/account-balances")
async def get_account_balances() -> dict:
    """Get cached Comdirect balances loaded during app startup."""
    return ComdirectBalanceService.get_balances_snapshot()


# ============================================================================
# API Routes - Classification
# ============================================================================


@router.post("/api/classify")
async def classify_transactions(
    transaction_ids: list[str] | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 25,
    persist: Annotated[bool, Query()] = False,
    use_cache: Annotated[bool, Query()] = True,
) -> dict:
    """
    Classify transactions through the Claude API.

    Consults the merchant cache first and only sends unknown merchants on, so
    a run over already-known transactions costs nothing.

    Query Parameters:
        transaction_ids: Specific transactions. Defaults to the ones still
            awaiting classification.
        limit: How many to take when no ids are given.
        persist: Write the results back. Off by default, so a run can be
            inspected before it changes anything.
        use_cache: Set to false to send every transaction to the API.

    Returns:
        The classification of each transaction and a summary of the run.
    """
    if transaction_ids:
        transactions = [
            transaction
            for transaction in [
                await Transaction.get(PydanticObjectId(tid)) for tid in transaction_ids
            ]
            if transaction is not None
        ]
    else:
        transactions = await (
            Transaction.find(Transaction.classification_source == SOURCE_PENDING)
            .sort("-date")
            .limit(limit)
            .to_list()
        )

    if not transactions:
        return {"status": "empty", "classified": 0, "results": []}

    try:
        run = await ClassificationService().classify(transactions, use_cache=use_cache)
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
async def upload_csv(files: list[UploadFile] = File(...)) -> dict:
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
    rules = await CategoryRule.find_all().to_list()
    categorizer = Categorizer(rules=rules)
    parser = Parser(rules=rules)

    imported_total = 0
    file_results = []
    imported_new: list[Transaction] = []

    for file in files:
        try:
            # Read and parse CSV
            contents = await file.read()
            transactions = parser.parse(contents)

            # Transactions are already categorized during parsing.
            # Insert new transactions, skip duplicates.
            file_imported = 0
            file_skipped = 0
            for transaction in transactions:
                existing = await Transaction.find_one(
                    Transaction.hash == transaction.hash
                )
                if existing:
                    file_skipped += 1
                    continue
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

    if imported_new:
        result.update(await _classify_import(imported_new))

    return result


async def _classify_import(transactions: list[Transaction]) -> dict:
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
        run = await ClassificationService().classify(transactions)
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
    transaction = await Transaction.get(PydanticObjectId(transaction_id))
    if not transaction:
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
    rules = await CategoryRule.find_all().to_list()
    categorizer = Categorizer(rules=rules)
    await CategorizationService.recompute_all_categories(
        categorizer,
        only_uncategorized=True,
    )

    return RedirectResponse(url="/categorize", status_code=303)
