#!/usr/bin/env python3
"""
Report how well the normalization collapses transactions onto merchants.

The distinct merchant count is the number the whole pipeline rests on: every
distinct key that is not already cached becomes an API call. If the count is
close to the transaction count, normalization is too weak and no amount of
batching will save the import.

The per-month table is the more useful half. It replays the statements in
order and counts, for each month, how many merchants had never been seen
before — which is exactly how many API calls a monthly import would make.

Usage:
    python scripts/merchant_report.py            # against DATABASE_NAME
    python scripts/merchant_report.py --top 40   # list the busiest merchants
"""

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.models import Transaction  # noqa: E402
from app.normalization import (  # noqa: E402
    build_canonical_map,
    is_unresolvable_paypal,
    merchant_key,
    normalize_merchant,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument(
        "--top", type=int, default=15, help="how many merchants to list"
    )
    parser.add_argument(
        "--months", type=int, default=12, help="how many months to break out"
    )
    args = parser.parse_args()

    await db_manager.connect()
    try:
        transactions = await Transaction.find_all().to_list()
    finally:
        await db_manager.disconnect()

    if not transactions:
        print(f"No transactions in {settings.database_name}.")
        return 1

    names = [normalize_merchant(t.booking_text, t.process) for t in transactions]
    canonical = build_canonical_map(name for name in names if name)
    keys = [merchant_key(canonical[name]) if name else "" for name in names]

    resolved = [key for key in keys if key]
    blind = sum(1 for t in transactions if is_unresolvable_paypal(t.booking_text))
    unresolved = len(keys) - len(resolved)

    print(f"database                 : {settings.database_name}")
    print(f"transactions             : {len(transactions)}")
    print(f"distinct merchant keys   : {len(set(resolved))}")
    ratio = len(transactions) / max(1, len(set(resolved)))
    print(f"collapse ratio           : {ratio:.1f} transactions per merchant")
    print(f"no merchant in the text  : {unresolved} ({blind} of them PayPal)")

    counts = Counter(resolved)
    print()
    print(f"--- {args.top} busiest merchants ---")
    for key, count in counts.most_common(args.top):
        display = next(
            (canonical[n] for n, k in zip(names, keys) if k == key and n), key
        )
        print(f"  {count:>5}  {display}")

    # Replay the statements month by month: a merchant is "new" the first
    # month it appears, which is when it would cost an API call.
    by_month: dict[str, list[str]] = defaultdict(list)
    for transaction, key in zip(transactions, keys):
        by_month[transaction.date.strftime("%Y-%m")].append(key)

    seen: set[str] = set()
    rows = []
    for month in sorted(by_month):
        month_keys = {key for key in by_month[month] if key}
        new = month_keys - seen
        rows.append((month, len(by_month[month]), len(month_keys), len(new)))
        seen |= month_keys

    recent = rows[-args.months :]
    print()
    print(f"--- last {len(recent)} months, replayed in order ---")
    print(f"{'month':<9}{'tx':>6}{'merchants':>11}{'new -> API':>12}{'cached':>9}")
    for month, tx_count, distinct, new in recent:
        share = (1 - new / distinct) * 100 if distinct else 100.0
        print(f"{month:<9}{tx_count:>6}{distinct:>11}{new:>12}{share:>8.0f}%")

    total_new = sum(row[3] for row in recent)
    total_distinct = sum(row[2] for row in recent)
    per_month = sorted(row[3] for row in recent)
    print()
    print(
        f"merchant cache hit rate  : "
        f"{(1 - total_new / max(1, total_distinct)) * 100:.0f}%"
    )
    print(
        f"new merchants per month  : median {per_month[len(per_month) // 2]}, "
        f"worst {per_month[-1]}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
