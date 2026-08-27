#!/usr/bin/env python3
"""
Give every merchant cache entry the direction it speaks for.

The cache used to hold one entry per merchant. That let the first direction
somebody categorised answer for both, and a merchant can mean two different
things depending on which way the money ran: maintenance received from a
person is income, a payment sent to that same person is not. Entries are now
identified by the merchant *and* the direction, so a merchant that runs both
ways can hold one answer per direction.

This does the two things ``init_beanie`` cannot:

*Backfills the direction* on the entries written before the field existed. The
direction they were learned from was never recorded, but the category implies
it — an entry filed under Income was learned from money coming in, anything
else from money going out. Transfers run both ways and mean the same thing
either way, so they keep the outgoing default; the lookup falls back across
the direction, so a single entry still answers both.

*Drops the old unique index* on ``merchant_key`` alone. It would reject the
second entry for a two-way merchant, which is the whole point of the change.
The compound unique index that replaces it is created by ``init_beanie``.

Usage:
    python scripts/migrate_merchant_directions.py --dry-run
    python scripts/migrate_merchant_directions.py
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.categories import flow_type_for  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import DIRECTION_IN, DIRECTION_OUT, MerchantCategory  # noqa: E402

#: The unique index the old one-entry-per-merchant shape needed.
OLD_INDEX = "merchant_key_1"
NEW_INDEX = "merchant_key_direction"


def direction_for(major: str) -> str:
    """The direction an entry written before the field existed was learned from."""
    return DIRECTION_IN if flow_type_for(major) == "income" else DIRECTION_OUT


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change"
    )
    args = parser.parse_args()

    # A plain driver connection rather than the app's, because Beanie creates
    # the declared indexes on startup and this script exists precisely to fix
    # the index state. Making the repair depend on the thing it repairs would
    # leave no way back once a database is in the wrong shape.
    client = AsyncIOMotorClient(settings.mongodb_url)
    try:
        collection = client[settings.database_name][MerchantCategory.Settings.name]
        indexes = await collection.index_information()

        # The field defaults to "out" on read, so an unmigrated entry is one
        # with nothing stored, not one whose stored value happens to be "out".
        stored = await collection.count_documents({"direction": {"$exists": True}})
        total = await collection.count_documents({})
        pending = await collection.find({"direction": {"$exists": False}}).to_list(
            length=None
        )

        planned: dict[str, int] = {DIRECTION_IN: 0, DIRECTION_OUT: 0}
        for entry in pending:
            planned[direction_for(entry.get("major", ""))] += 1

        print(f"database        : {settings.database_name}")
        print(f"merchant entries: {total} ({stored} already carry a direction)")
        print(f"to backfill     : {len(pending)}")
        print(f"  incoming      : {planned[DIRECTION_IN]}")
        print(f"  outgoing      : {planned[DIRECTION_OUT]}")
        print(
            f"old unique index: "
            f"{'present, will be dropped' if OLD_INDEX in indexes else 'already gone'}"
        )

        if planned[DIRECTION_IN]:
            print("\nentries the backfill reads as incoming:")
            for entry in pending:
                if direction_for(entry.get("major", "")) == DIRECTION_IN:
                    print(
                        f"  {entry.get('display_name', '')[:44]:<44}  "
                        f"{entry.get('major')} / {entry.get('minor')}"
                    )

        if args.dry_run:
            print("\nDry run — nothing written.")
            return 0

        for direction in (DIRECTION_IN, DIRECTION_OUT):
            majors = [
                major
                for major in {entry.get("major", "") for entry in pending}
                if direction_for(major) == direction
            ]
            if not majors:
                continue
            await collection.update_many(
                {"direction": {"$exists": False}, "major": {"$in": majors}},
                {"$set": {"direction": direction}},
            )

        if OLD_INDEX in indexes:
            await collection.drop_index(OLD_INDEX)
            print(f"dropped index {OLD_INDEX}")

        # Declared in the model and normally created by init_beanie; asked
        # for here too so a run leaves the collection in its final shape
        # rather than waiting for the next startup.
        await collection.create_index(
            [("merchant_key", 1), ("direction", 1)],
            unique=True,
            name=NEW_INDEX,
        )

        remaining = await collection.count_documents({"direction": {"$exists": False}})
        print(f"\nbackfilled {len(pending)}, {remaining} still without a direction")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
