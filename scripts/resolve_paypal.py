#!/usr/bin/env python3
"""
Re-file PayPal transactions whose payee was recovered from a PayPal export.

New imports pick the payee up on their own — the classifier consults the
PayPal ledger as part of its funnel. This script is for the transactions that
were already in the database when the export arrived, and which are therefore
sitting on the ``fallback`` category the pipeline gives a line it cannot read.

Each recovered payee is put through the ordinary classifier, so it lands in
the same category the merchant would have had if Comdirect had named it, gets
cached under that merchant, and can be corrected in the UI like any other.

Resumable: a transaction is only touched when the ledger can name its payee,
and the categories come from the merchant cache once a merchant is known, so
stopping and re-running costs at most the merchants that had not been reached.

Usage:
    python scripts/resolve_paypal.py --dry-run
    python scripts/resolve_paypal.py --limit 25
    python scripts/resolve_paypal.py
"""

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classification.client import ClassificationError  # noqa: E402
from app.classification.service import (  # noqa: E402
    ClassificationService,
    apply,
    report_lines,
)
from app.config import get_logger, settings  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.models import Transaction  # noqa: E402
from app.normalization import is_unresolvable_paypal  # noqa: E402
from app.paypal import has_records, load_ledger  # noqa: E402

logger = get_logger(__name__)


async def truncated_paypal_transactions(limit: int | None) -> list[Transaction]:
    """
    Every PayPal line Comdirect truncated the payee out of, oldest first.

    Filtered in Python rather than in a query: whether a line is truncated is
    decided by ``app.normalization``, whose several wrapped spellings of "Ihr
    Einkauf bei" are not something to restate as a Mongo regex and keep in
    step by hand.
    """
    transactions = [
        transaction
        async for transaction in Transaction.find_all().sort("date")
        if is_unresolvable_paypal(transaction.booking_text)
    ]
    return transactions[:limit] if limit else transactions


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be recovered, without calling the API or writing",
    )
    parser.add_argument("--limit", type=int, help="only take this many transactions")
    parser.add_argument(
        "--no-escalation",
        action="store_true",
        help="skip the second pass with the stronger model",
    )
    args = parser.parse_args()

    await db_manager.connect()
    try:
        print(f"database              : {settings.database_name}")
        if not await has_records():
            print(
                "\nNo PayPal export has been imported yet.\n"
                "Run: python scripts/import_paypal.py <folder>"
            )
            return 1

        ledger = await load_ledger()
        transactions = await truncated_paypal_transactions(args.limit)
        print(f"PayPal settlements    : {len(ledger.settlements)}")
        print(f"truncated PayPal lines: {len(transactions)}")

        if not transactions:
            print("\nNothing to do — no truncated PayPal lines in the database.")
            return 0

        matched, unmatched = [], []
        payees: Counter[str] = Counter()
        for transaction in transactions:
            match = ledger.resolve(transaction.amount, transaction.date.date())
            if match is None:
                unmatched.append(transaction)
            else:
                matched.append(transaction)
                payees[match.counterparty] += 1

        rate = len(matched) / len(transactions) * 100
        print(f"payee recovered       : {len(matched)} ({rate:.1f}%)")
        print(f"still unrecoverable   : {len(unmatched)}")

        if payees:
            print("\nmost common recovered payees:")
            for name, count in payees.most_common(10):
                print(f"  {count:4d}  {name}")

        if unmatched:
            print("\nno unambiguous match (left on the fallback category):")
            for transaction in unmatched[:15]:
                booked = transaction.date.date()
                fits = len(ledger.candidates(transaction.amount, booked))
                print(f"  {booked}  {transaction.amount:>10.2f}  {fits} candidate(s)")
            if len(unmatched) > 15:
                print(f"  ... and {len(unmatched) - 15} more")

        if not matched:
            print("\nNothing to re-file.")
            return 0

        if args.dry_run:
            print(
                "\nDry run — no API calls, nothing written. On a real run the "
                "recovered payees go through the classifier, and merchants "
                "already in the cache cost no request."
            )
            return 0

        try:
            service = ClassificationService(
                escalate=not args.no_escalation,
                paypal_ledger=ledger,
            )
            run = await service.classify(matched)
        except ClassificationError as error:
            print(f"\n{error}")
            return 2

        written = await apply(run.classifications, matched)

        print()
        print("\n".join(report_lines(run, len(matched))))
        print()
        print(f"written               : {written}")
        return 0
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
