"""
API route handlers for the FastAPI application.
"""
from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.categorizer import Categorizer
from app.config import get_logger
from app.constants import CATEGORY_HIERARCHY
from app.models import CategoryRule, Transaction
from app.parser import Parser
from app.services import (
    CategorizationService,
    ChartDataService,
    ComdirectBalanceService,
)

logger = get_logger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Exposed to every template: the frontend derives its category colour palette
# from this hierarchy instead of maintaining a parallel list of hex values.
templates.env.globals["CATEGORY_HIERARCHY"] = CATEGORY_HIERARCHY


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

    return {
        "status": "success",
        "imported": imported_total,
        "files": file_results,
    }


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
