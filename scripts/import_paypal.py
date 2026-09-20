#!/usr/bin/env python3
"""
Import PayPal activity exports so truncated bank lines can find their payee.

Comdirect drops the merchant from many PayPal booking lines. A PayPal activity
export knows who was paid; this script stores those rows so
:mod:`app.paypal` can join the two. Nothing is categorised here — importing
only makes the evidence available. Run ``resolve_paypal.py`` afterwards to
apply it to transactions that are already in the database.

Exports overlap heavily, and PayPal's transaction code is the identity, so
running this repeatedly over the same folder is a no-op after the first time.

Getting the files: PayPal → Activity → Download, "Balance affecting" or
"All transactions", CSV. Download one year at a time; the export caps the
range. Either locale works.

Usage:
    python scripts/import_paypal.py paypal_data/
    python scripts/import_paypal.py paypal_data/*.CSV
    python scripts/import_paypal.py paypal_data/ --dry-run
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_logger, settings  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.models import User  # noqa: E402
from app.paypal import PayPalFormatError, load_ledger, parse_export  # noqa: E402
from app.paypal.store import store_rows  # noqa: E402

logger = get_logger(__name__)

#: Suffixes a PayPal export arrives with. Matched case-insensitively, because
#: PayPal serves ".CSV" from the web export and ".csv" from email.
SUFFIXES = {".csv"}


def collect(paths: list[str]) -> list[Path]:
    """Every CSV under the given files and folders, in a stable order."""
    found: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            found += [
                child
                for child in path.iterdir()
                if child.is_file() and child.suffix.lower() in SUFFIXES
            ]
        elif path.is_file():
            found.append(path)
        else:
            logger.warning("Skipping %s: not a file or directory", path)
    return sorted(set(found))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="whose PayPal ledger this is")
    parser.add_argument("paths", nargs="+", help="CSV files or folders holding them")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and report, without writing anything",
    )
    args = parser.parse_args()

    files = collect(args.paths)
    if not files:
        print("No CSV files found.")
        return 1

    print(f"Database: {settings.database_name}")
    print(f"Files   : {len(files)}\n")

    parsed: dict[Path, list] = {}
    for path in files:
        try:
            rows = parse_export(path.read_bytes())
        except PayPalFormatError as error:
            # One unreadable file must not strand the others: a folder often
            # holds an unrelated CSV, and reporting it beats refusing the lot.
            print(f"  {path.name}: skipped — {error}")
            continue
        parsed[path] = rows
        span = f"{rows[0].date} .. {rows[-1].date}" if rows else "empty"
        print(f"  {path.name}: {len(rows):5d} rows  {span}")

    every_row = [row for rows in parsed.values() for row in rows]
    if not every_row:
        print("\nNothing readable found.")
        return 1

    if args.dry_run:
        codes = {row.code for row in every_row}
        print(f"\nDry run: {len(every_row)} rows, {len(codes)} distinct.")
        return 0

    await db_manager.connect()
    try:
        user = await User.find_one(User.username == args.username)
        if user is None:
            print(f"No user {args.username!r}.")
            return 1

        result = await store_rows(user.id, every_row)
        print(f"\n{result.summary()}")

        ledger = await load_ledger(user.id)
        named = [s for s in ledger.settlements if s.counterparty]
        print(
            f"Ledger now holds {len(ledger.settlements)} bank settlements, "
            f"{len(named)} of them naming a payee."
        )
        print("\nNext: python scripts/resolve_paypal.py --dry-run")
    finally:
        await db_manager.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
