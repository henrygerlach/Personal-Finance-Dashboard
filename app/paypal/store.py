"""
Keeping PayPal exports in the database and handing back a ledger.

Consecutive PayPal downloads overlap heavily — seven exports of one account
shared 130 rows — so importing is defined as "make sure these rows are known"
rather than "add these rows". PayPal's own transaction code is the identity,
which makes re-importing the same file a no-op and re-importing a wider date
range cost only its new rows.

The ledger is rebuilt from the stored rows rather than cached across requests.
It is a few hundred settlements over a decade of history; holding a stale one
would cost more in confusion than rebuilding one costs in time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import get_logger
from app.models import PayPalRecord
from app.paypal.ledger import PayPalLedger
from app.paypal.records import PayPalRow, parse_export

logger = get_logger(__name__)


@dataclass
class ImportResult:
    """What one import run changed."""

    read: int = 0
    added: int = 0
    already_known: int = 0

    def summary(self) -> str:
        return (
            f"{self.read} rows read, {self.added} added, "
            f"{self.already_known} already known"
        )


async def store_rows(rows: list[PayPalRow]) -> ImportResult:
    """
    Add whatever is new, leave whatever is known.

    A row already in the database is never rewritten. The export is a
    historical record: if PayPal reports a row differently in a later
    download, the earlier reading is the one that was current when the
    matching bank statement was imported, and overwriting it would silently
    re-point matches that have already been made.
    """
    result = ImportResult(read=len(rows))
    if not rows:
        return result

    # One query for the whole file rather than one per row: an export is a
    # few hundred rows and almost all of them are usually already known.
    codes = [row.code for row in rows]
    known = {
        document.code
        for document in await PayPalRecord.find({"code": {"$in": codes}}).to_list()
    }

    fresh: list[PayPalRecord] = []
    seen: set[str] = set()
    for row in rows:
        if row.code in known or row.code in seen:
            result.already_known += 1
            continue
        seen.add(row.code)
        fresh.append(
            PayPalRecord(
                code=row.code,
                date=datetime(row.date.year, row.date.month, row.date.day),
                kind=row.kind,
                status=row.status,
                name=row.name,
                currency=row.currency,
                gross=row.gross,
                related_code=row.related_code,
                item_title=row.item_title,
            )
        )

    if fresh:
        await PayPalRecord.insert_many(fresh)
        result.added = len(fresh)
    logger.info("PayPal import: %s", result.summary())
    return result


async def import_export(payload: bytes | str) -> ImportResult:
    """Read one PayPal activity export and store whatever is new in it."""
    return await store_rows(parse_export(payload))


async def load_ledger(**options) -> PayPalLedger:
    """
    A ledger over every PayPal row stored so far.

    Options are passed to :class:`~app.paypal.ledger.PayPalLedger`, so a
    caller that wants a different settlement window can ask for one.
    """
    documents = await PayPalRecord.find_all().to_list()
    rows = [
        PayPalRow(
            code=document.code,
            date=document.date.date(),
            kind=document.kind,
            status=document.status,
            name=document.name,
            currency=document.currency,
            gross=document.gross,
            related_code=document.related_code,
            item_title=document.item_title,
        )
        for document in documents
    ]
    return PayPalLedger(rows, **options)


async def has_records() -> bool:
    """Whether any PayPal export has been imported yet."""
    return await PayPalRecord.find_all().count() > 0
