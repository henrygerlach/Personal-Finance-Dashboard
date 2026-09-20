#!/usr/bin/env python3
"""
Assign every document that predates login to one account.

Every collection except ``users`` and ``comdirect_balance_snapshots`` gained a
required ``user_id`` when accounts were added. Documents written before that
have no such field, so Beanie can't even parse them back out. This bypasses
Beanie and sets the field directly with the driver, once, for whichever
username should own the pre-existing data.

Usage:
    python scripts/assign_existing_data.py alice --dry-run   # count, write nothing
    python scripts/assign_existing_data.py alice             # assign

Take a backup first; see backups/README.md.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db_manager  # noqa: E402
from app.models import User  # noqa: E402

#: Every collection carrying a user_id, per app/models.py.
COLLECTIONS = [
    "transactions",
    "category_rules",
    "merchant_categories",
    "paypal_records",
    "recurring_overrides",
    "trips",
    "trip_assignments",
    "dismissed_trip_proposals",
    "pattern_dismissals",
]


async def assign(user_id, dry_run: bool) -> int:
    database = db_manager.database
    orphaned = {"user_id": {"$exists": False}}
    for name in COLLECTIONS:
        count = await database[name].count_documents(orphaned)
        if dry_run:
            print(f"{name:<25} {count} would be assigned")
            continue
        result = await database[name].update_many(orphaned, {"$set": {"user_id": user_id}})
        print(f"{name:<25} {result.modified_count} assigned")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("username", help="who should own the pre-existing data")
    parser.add_argument(
        "--dry-run", action="store_true", help="count what would change, write nothing"
    )
    args = parser.parse_args()

    await db_manager.connect()
    try:
        user = await User.find_one(User.username == args.username)
        if user is None:
            print(f"No user {args.username!r}.")
            return 1
        return await assign(user.id, args.dry_run)
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
