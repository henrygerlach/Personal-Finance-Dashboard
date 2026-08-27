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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.config import get_logger
from app.models import PayPalRecord
from app.paypal.ledger import PayPalLedger, Settlement
from app.paypal.records import PayPalRow, parse_export

logger = get_logger(__name__)


@dataclass
class ImportResult:
    """What one import run changed."""

    read: int = 0
    added: int = 0
    already_known: int = 0
    detailed: int = 0

    def summary(self) -> str:
        text = (
            f"{self.read} rows read, {self.added} added, "
            f"{self.already_known} already known"
        )
        if self.detailed:
            text += f", {self.detailed} filled in with sender/message detail"
        return text


async def store_rows(rows: list[PayPalRow]) -> ImportResult:
    """
    Add whatever is new, leave whatever is known.

    A row already in the database is never rewritten. The export is a
    historical record: if PayPal reports a row differently in a later
    download, the earlier reading is the one that was current when the
    matching bank statement was imported, and overwriting it would silently
    re-point matches that have already been made.

    The one exception is a field that is *empty* and carries no matching
    weight — sender, receiver, subject and note. Rows stored before those
    columns were read have them blank, and leaving them blank forever would
    mean re-importing could never surface the message on a payment. Filling
    an empty field in cannot re-point anything, because nothing matches on
    it; a field that already holds a value is still left alone.
    """
    result = ImportResult(read=len(rows))
    if not rows:
        return result

    # One query for the whole file rather than one per row: an export is a
    # few hundred rows and almost all of them are usually already known.
    codes = [row.code for row in rows]
    stored = {
        document.code: document
        for document in await PayPalRecord.find({"code": {"$in": codes}}).to_list()
    }
    known = set(stored)

    fresh: list[PayPalRecord] = []
    seen: set[str] = set()
    for row in rows:
        if row.code in known or row.code in seen:
            result.already_known += 1
            if row.code in stored and await _fill_in_detail(stored[row.code], row):
                result.detailed += 1
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
                sender=row.sender,
                receiver=row.receiver,
                subject=row.subject,
                note=row.note,
            )
        )

    if fresh:
        await PayPalRecord.insert_many(fresh)
        result.added = len(fresh)
    logger.info("PayPal import: %s", result.summary())
    return result


#: Fields the export carries for a reader's benefit only. Nothing matches on
#: them, which is what makes filling an empty one in on a known row safe.
_DETAIL_FIELDS = ("sender", "receiver", "subject", "note")


async def _fill_in_detail(document: PayPalRecord, row: PayPalRow) -> bool:
    """
    Put a message or an address onto a row that was stored without one.

    Only ever writes over an empty field, so re-importing an export cannot
    change what a stored row already says.

    Returns:
        Whether anything was written.
    """
    changed = False
    for field in _DETAIL_FIELDS:
        value = getattr(row, field, "")
        if value and not getattr(document, field, ""):
            setattr(document, field, value)
            changed = True
    if changed:
        await document.save()
    return changed


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
            sender=document.sender,
            receiver=document.receiver,
            subject=document.subject,
            note=document.note,
        )
        for document in documents
    ]
    return PayPalLedger(rows, **options)


async def settlement_details(codes: Iterable[str]) -> dict[str, Settlement]:
    """
    What PayPal knew about each of these settlements, keyed by its code.

    A transaction matched to PayPal records the settlement code it was matched
    to, which is how a page gets back to the export without repeating the
    search that found it. Codes the ledger does not know are simply absent
    from the result — an export can be narrower than the statement.

    One ledger is built for the whole batch rather than one per transaction:
    the review page asks about fifty at a time.
    """
    wanted = {code for code in codes if code}
    if not wanted:
        return {}

    ledger = await load_ledger()
    found: dict[str, Settlement] = {}
    for code in wanted:
        settlement = ledger.settlement_for(code)
        if settlement is not None:
            found[code] = settlement
    return found


async def has_records() -> bool:
    """Whether any PayPal export has been imported yet."""
    return await PayPalRecord.find_all().count() > 0
