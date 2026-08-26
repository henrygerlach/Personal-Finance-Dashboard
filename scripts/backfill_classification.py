#!/usr/bin/env python3
"""
Classify the transactions the taxonomy migration could not map.

Four legacy buckets had no honest target and were left marked ``pending``
holding a provisional category. This script sends them through the classifier
and replaces those provisional values with real ones.

It is deliberately synchronous rather than using the Message Batches API.
After deduplication the whole backfill is a few hundred merchants — around
twenty requests, a couple of minutes — and the batch API would add submit,
poll and retrieve logic plus a second code path to test, to save well under a
dollar. If the merchant count ever grows into a range where that trade turns
around, this is the place to change it.

Resumable: work is keyed on merchants, and a merchant already in the cache is
skipped, so the script can be stopped and re-run at any point.

Usage:
    python scripts/backfill_classification.py --dry-run
    python scripts/backfill_classification.py --limit 50
    python scripts/backfill_classification.py
"""

import argparse
import asyncio
import sys
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
from app.models import SOURCE_PENDING, Transaction  # noqa: E402
from app.normalization import (  # noqa: E402
    build_canonical_map,
    merchant_key,
    normalize_merchant,
)

logger = get_logger(__name__)


async def pending_transactions(limit: int | None) -> list[Transaction]:
    """Transactions still holding a provisional category, oldest first."""
    query = Transaction.find(Transaction.classification_source == SOURCE_PENDING).sort(
        "date"
    )
    if limit:
        query = query.limit(limit)
    return await query.to_list()


def distinct_merchants(transactions) -> int:
    """How many merchants the transactions collapse onto."""
    names = [normalize_merchant(t.booking_text, t.process) for t in transactions]
    canonical = build_canonical_map(name for name in names if name)
    return len({merchant_key(canonical[name]) for name in names if name})


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the work without calling the API or writing",
    )
    parser.add_argument("--limit", type=int, help="only take this many transactions")
    parser.add_argument(
        "--model", help="override the classification model for this run"
    )
    parser.add_argument(
        "--no-escalation",
        action="store_true",
        help="skip the second pass with the stronger model",
    )
    args = parser.parse_args()

    await db_manager.connect()
    try:
        transactions = await pending_transactions(args.limit)
        print(f"database             : {settings.database_name}")
        print(f"pending transactions : {len(transactions)}")

        if not transactions:
            print("\nNothing to do — every transaction has a real category.")
            return 0

        merchants = distinct_merchants(transactions)
        print(f"distinct merchants   : {merchants}")
        print(f"model                : {args.model or settings.classification_model}")

        if args.dry_run:
            print(
                "\nDry run — no API calls, nothing written. Merchants already "
                "in the cache would be skipped on a real run, so the number of "
                "requests will be lower than the merchant count above."
            )
            return 0

        if args.model:
            settings.classification_model = args.model

        try:
            service = ClassificationService(
                escalate=not args.no_escalation,
            )
            run = await service.classify(transactions)
        except ClassificationError as error:
            print(f"\n{error}")
            return 2

        written = await apply(run.classifications, transactions)

        print()
        print("\n".join(report_lines(run, len(transactions))))
        print()
        print(f"written              : {written}")

        remaining = await Transaction.find(
            Transaction.classification_source == SOURCE_PENDING
        ).count()
        print(f"still pending        : {remaining}")
        if remaining:
            print("Re-run to continue; merchants already cached are skipped.")
        return 0
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
