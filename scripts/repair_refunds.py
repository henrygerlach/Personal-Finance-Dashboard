#!/usr/bin/env python3
"""
Re-file transactions whose category disagrees with their direction.

A category says what a payment was for and is remembered per merchant; the
direction belongs to the single transaction. Where the two disagree, the
category is wrong — an Adidas return is not spending on clothes, and a
supermarket credit is not groceries.

The classifier applies this rule to everything it decides, but transactions
that never went through it do not have it: the ones migrated from the old rule
system carry categories assigned by hand, and a refund among them was filed
under whatever the purchase would have been. That is what this repairs.

Two cases, and only one of them is repairable:

*An incoming amount under an expense category* is a refund. The correct
category is derivable, so it is rewritten.

*An outgoing amount under refunds* cannot be repaired, because the category it
should have had is not recoverable. Those are handed back to the classifier by
marking them pending; ``backfill_classification.py`` picks them up.

Usage:
    python scripts/repair_refunds.py --dry-run
    python scripts/repair_refunds.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.categories import flow_type_for  # noqa: E402
from app.classification.service import (  # noqa: E402
    is_misfiled_refund,
    refund_target,
)
from app.config import settings  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.models import SOURCE_PENDING, Transaction  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change"
    )
    args = parser.parse_args()

    await db_manager.connect()
    try:
        transactions = await Transaction.find_all().to_list()

        refunds = [
            (t, refund_target(t.major_category, t.minor_category, t.amount))
            for t in transactions
        ]
        refunds = [(t, target) for t, target in refunds if target is not None]
        unrepairable = [
            t
            for t in transactions
            if is_misfiled_refund(t.major_category, t.minor_category, t.amount)
        ]

        print(f"database             : {settings.database_name}")
        print(f"transactions         : {len(transactions)}")
        print()

        if refunds:
            print(f"credits filed as spending: {len(refunds)}")
            for transaction, target in sorted(refunds, key=lambda r: -r[0].amount):
                print(
                    f"  {transaction.date:%Y-%m-%d} {transaction.amount:>9.2f}  "
                    f"{transaction.major_category} / {transaction.minor_category}"
                    f"  ->  {target[0]} / {target[1]}"
                )
            total = sum(t.amount for t, _ in refunds)
            print(f"  {total:.2f} EUR moved out of the expense charts")
        else:
            print("credits filed as spending: none")

        print()
        if unrepairable:
            print(
                f"debits filed as refunds  : {len(unrepairable)} "
                "(not repairable, handed back to the classifier)"
            )
            for transaction in unrepairable:
                print(
                    f"  {transaction.date:%Y-%m-%d} {transaction.amount:>9.2f}  "
                    f"{(transaction.booking_text or '')[:64]}"
                )
        else:
            print("debits filed as refunds  : none")

        if args.dry_run:
            print("\nDry run — nothing written.")
            return 0

        for transaction, target in refunds:
            transaction.major_category, transaction.minor_category = target
            transaction.flow_type = flow_type_for(target[0])
            transaction.reason = (
                "Incoming amount at a merchant otherwise filed as spending, "
                "so a refund."
            )
            await transaction.save()

        for transaction in unrepairable:
            # The category is not recoverable, so it goes back to the
            # classifier rather than being guessed at here.
            transaction.classification_source = SOURCE_PENDING
            transaction.confidence = None
            transaction.reason = (
                "Was filed as a refund despite being an outgoing payment."
            )
            await transaction.save()

        print(
            f"\nre-filed {len(refunds)}, marked {len(unrepairable)} "
            "for re-classification"
        )
        if unrepairable:
            print("Run scripts/backfill_classification.py to finish those.")
        return 0
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
