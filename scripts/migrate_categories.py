#!/usr/bin/env python3
"""
Migrate transactions from the legacy category hierarchy to the new taxonomy.

The 2,600-odd hand-categorized transactions are the most valuable thing in the
database, so this script is built to be undone. It writes the old category
into ``legacy_major`` / ``legacy_minor`` before overwriting it, which makes the
run both reversible and idempotent: a transaction that already carries a
legacy category has been migrated and is left alone.

What it does, in order:

1. Translates each transaction's category through ``app.legacy_mapping``.
   A pair the mapping does not cover is reported and skipped, never guessed.
2. Derives ``flow_type`` from the new major category.
3. Marks the four legacy buckets that have no honest target as ``pending``,
   so the classifier can revisit them later.
4. Fills the merchant cache from the migrated transactions, so the hand-made
   categories answer lookups before the API is ever called.

Usage:
    python scripts/migrate_categories.py --dry-run    # report, write nothing
    python scripts/migrate_categories.py              # migrate
    python scripts/migrate_categories.py --rollback   # restore the old state

Take a backup first; see backups/README.md.
"""

import argparse
import asyncio
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.categories import code_for, flow_type_for, is_valid_pair  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.legacy_mapping import (  # noqa: E402
    invalid_targets,
    map_pair,
    unmapped_legacy_pairs,
)
from app.models import (  # noqa: E402
    SOURCE_LEGACY_RULES,
    SOURCE_PENDING,
    SOURCE_USER,
    MerchantCategory,
    Transaction,
    User,
)
from app.normalization import (  # noqa: E402
    build_canonical_map,
    merchant_key,
    normalize_merchant,
)

# Defaults the legacy model used when nothing was assigned.
LEGACY_DEFAULT = ("Other", "Uncategorized")

#: How decisive a merchant's hand-made categories must be before the
#: majority is cached despite the disagreement.
MAJORITY_SHARE = 2 / 3
MAJORITY_MIN_TRANSACTIONS = 3


def _legacy_pair_of(transaction: Transaction) -> tuple[str, str]:
    """The transaction's current category, with the legacy defaults applied."""
    major = (transaction.major_category or LEGACY_DEFAULT[0]).strip()
    minor = (transaction.minor_category or LEGACY_DEFAULT[1]).strip()
    return major, minor


async def migrate(user_id, dry_run: bool) -> tuple[int, list]:
    """
    Translate every unmigrated transaction.

    Returns:
        The process exit code and the planned changes as
        ``(transaction, new_pair, provisional)`` triples, so a dry run can
        preview the merchant cache without having written anything.
    """
    transactions = await Transaction.find(Transaction.user_id == user_id).to_list()

    already = [t for t in transactions if t.legacy_major is not None]
    pending = [t for t in transactions if t.legacy_major is None]

    print(f"transactions        : {len(transactions)}")
    print(f"already migrated    : {len(already)} (skipped)")
    print(f"to migrate          : {len(pending)}")
    print()

    if not pending:
        print("Nothing to do.")
        return 0, []

    changes: list[tuple[Transaction, tuple[str, str], bool]] = []
    unmapped: Counter = Counter()
    already_new = 0

    for transaction in pending:
        legacy = _legacy_pair_of(transaction)
        # A transaction imported after the switch-over was categorised by the
        # classifier and is already in the new taxonomy. It has no legacy
        # category and needs no migration — without this it would be reported
        # as an unmapped legacy pair, which is alarming and wrong.
        if is_valid_pair(*legacy):
            already_new += 1
            continue
        try:
            new_pair, provisional = map_pair(*legacy)
        except KeyError:
            unmapped[legacy] += 1
            continue
        changes.append((transaction, new_pair, provisional))

    if already_new:
        print(f"already in the new taxonomy: {already_new} (skipped)")

    # --- Report -------------------------------------------------------------
    moves: Counter = Counter()
    for transaction, new_pair, provisional in changes:
        moves[(_legacy_pair_of(transaction), new_pair, provisional)] += 1

    print(f"{'legacy':<34} {'new':<48} {'n':>5}  state")
    print("-" * 100)
    for (legacy, new_pair, provisional), count in sorted(
        moves.items(), key=lambda item: -item[1]
    ):
        state = "pending" if provisional else "final"
        print(
            f"{legacy[0] + ' / ' + legacy[1]:<34} "
            f"{new_pair[0] + ' / ' + new_pair[1]:<48} {count:>5}  {state}"
        )
    print()

    flows = Counter(flow_type_for(new_pair[0]) for _, new_pair, _ in changes)
    print(
        "flow types          : "
        + ", ".join(f"{k}={v}" for k, v in sorted(flows.items()))
    )
    provisional_count = sum(1 for _, _, p in changes if p)
    print(f"marked pending      : {provisional_count}")
    print()

    if unmapped:
        print("!! UNMAPPED LEGACY CATEGORIES — decide these, they were not touched:")
        for (major, minor), count in unmapped.most_common():
            print(f"   {major} / {minor}: {count} transactions")
        print()

    if dry_run:
        print("Dry run — nothing written.")
        return (1 if unmapped else 0), changes

    # --- Write --------------------------------------------------------------
    for transaction, new_pair, provisional in changes:
        legacy_major, legacy_minor = _legacy_pair_of(transaction)
        transaction.legacy_major = legacy_major
        transaction.legacy_minor = legacy_minor
        transaction.major_category, transaction.minor_category = new_pair
        transaction.flow_type = flow_type_for(new_pair[0])
        transaction.classification_source = (
            SOURCE_PENDING if provisional else SOURCE_LEGACY_RULES
        )
        await transaction.save()

    print(f"Migrated {len(changes)} transactions.")
    return (1 if unmapped else 0), changes


async def fill_merchant_cache(
    user_id, dry_run: bool, planned: list | None = None
) -> None:
    """
    Seed the merchant cache from the migrated transactions.

    Only finally-categorized transactions contribute — a provisional category
    is not an answer and must not be cached as one. Entries are written as
    ``user`` because they come from categories assigned by hand, which is
    exactly the authority a user correction carries.

    A merchant whose transactions disagree about the category is skipped: with
    no way to tell which of them is right, the classifier should look at it.
    """
    if dry_run:
        # Nothing was written, so the planned changes are the only source of
        # the categories this would cache.
        rows = [
            (transaction, new_pair)
            for transaction, new_pair, provisional in (planned or [])
            if not provisional
        ]
    else:
        # After a real run the database is authoritative, and it also covers
        # transactions migrated by an earlier run.
        rows = [
            (transaction, (transaction.major_category, transaction.minor_category))
            for transaction in await Transaction.find(
                Transaction.user_id == user_id,
                Transaction.classification_source == SOURCE_LEGACY_RULES,
            ).to_list()
        ]

    named = [
        (normalize_merchant(transaction.booking_text, transaction.process), pair)
        for transaction, pair in rows
    ]
    # Fold town and legal-form suffixes onto the shorter spelling first, so a
    # shop that a card payment writes as "EDEKA BOECKER HAMBURG" and a direct
    # debit as "EDEKA BOECKER" ends up as one entry rather than two that can
    # drift into different categories.
    canonical = build_canonical_map(name for name, _ in named if name)

    by_merchant: dict[str, Counter] = defaultdict(Counter)
    display: dict[str, str] = {}
    for name, pair in named:
        if not name:
            continue
        name = canonical[name]
        key = merchant_key(name)
        display.setdefault(key, name)
        by_merchant[key][pair] += 1

    # A merchant whose transactions disagree is not automatically unusable.
    # Every conflict in the live data comes from one mapping decision — the
    # legacy "Transfers / External" bucket maps to Person to Person, and it
    # contains occasional payments from people who otherwise send maintenance
    # or rent. The hand-made majority is far better evidence than anything a
    # classifier could produce, and leaving these uncached let the classifier
    # overwrite 53 hand-made decisions with one guess.
    #
    # A clear majority is taken; a genuine split is still left alone.
    agreed: dict[str, Counter] = {}
    conflicted: dict[str, Counter] = {}
    for merchant, counter in by_merchant.items():
        total = sum(counter.values())
        (_, top_count) = counter.most_common(1)[0]
        if len(counter) == 1:
            agreed[merchant] = counter
        elif total >= MAJORITY_MIN_TRANSACTIONS and top_count / total >= MAJORITY_SHARE:
            agreed[merchant] = counter
        else:
            conflicted[merchant] = counter

    print()
    print(f"merchants seen      : {len(by_merchant)}")
    print(f"cacheable           : {len(agreed)}")
    print(f"genuinely split     : {len(conflicted)} (skipped, left to the classifier)")

    if conflicted:
        print()
        print("  the five most frequent conflicts:")
        ranked = sorted(conflicted.items(), key=lambda i: -sum(i[1].values()))
        for merchant, counter in ranked[:5]:
            spread = ", ".join(
                f"{major}/{minor}×{n}" for (major, minor), n in counter.most_common()
            )
            print(f"    {display[merchant]}: {spread}")

    if dry_run:
        print()
        print(f"Dry run — would write {len(agreed)} merchant entries.")
        return

    existing = {
        entry.merchant_key: entry
        for entry in await MerchantCategory.find(
            MerchantCategory.user_id == user_id
        ).to_list()
    }
    written = 0
    for merchant, counter in agreed.items():
        (major, minor), hits = counter.most_common(1)[0]
        entry = existing.get(merchant)
        now = datetime.utcnow()
        if entry is None:
            await MerchantCategory(
                user_id=user_id,
                merchant_key=merchant,
                display_name=display[merchant],
                major=major,
                minor=minor,
                category_code=code_for(major, minor),
                source=SOURCE_USER,
                hit_count=hits,
                first_seen=now,
                last_used=now,
                # These categories were assigned by hand, not by a model.
                model_version=None,
            ).insert()
        else:
            entry.display_name = display[merchant]
            entry.major, entry.minor = major, minor
            entry.category_code = code_for(major, minor)
            entry.source = SOURCE_USER
            entry.hit_count = hits
            entry.last_used = now
            await entry.save()
        written += 1

    print()
    print(f"Wrote {written} merchant cache entries.")


async def rollback(user_id, dry_run: bool) -> int:
    """Restore the legacy categories and drop everything the migration derived."""
    transactions = await Transaction.find(
        Transaction.user_id == user_id,
        Transaction.legacy_major != None,  # noqa: E711 — Beanie builds a query here
    ).to_list()
    merchants = await MerchantCategory.find(MerchantCategory.user_id == user_id).count()

    print(f"migrated transactions: {len(transactions)}")
    print(f"merchant cache entries: {merchants} (all will be removed)")

    if dry_run:
        print("\nDry run — nothing written.")
        return 0

    if not transactions and not merchants:
        print("\nNothing to roll back.")
        return 0

    for transaction in transactions:
        transaction.major_category = transaction.legacy_major
        transaction.minor_category = transaction.legacy_minor
        transaction.legacy_major = None
        transaction.legacy_minor = None
        transaction.flow_type = None
        transaction.classification_source = None
        transaction.confidence = None
        transaction.reason = None
        await transaction.save()

    # The cache is derived entirely from the migration at this point, so it
    # goes with it. Once corrections can be made in the UI this becomes
    # destructive and needs revisiting.
    await MerchantCategory.find(MerchantCategory.user_id == user_id).delete()

    print(f"\nRestored {len(transactions)} transactions, cleared the merchant cache.")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("username", help="whose transactions to migrate")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="restore legacy categories and clear the merchant cache",
    )
    args = parser.parse_args()

    # A broken mapping table is a programming error, not a data problem.
    assert (
        not unmapped_legacy_pairs()
    ), f"legacy pairs without a mapping: {sorted(unmapped_legacy_pairs())}"
    assert (
        not invalid_targets()
    ), f"mapping targets outside the new hierarchy: {sorted(invalid_targets())}"

    await db_manager.connect()
    try:
        user = await User.find_one(User.username == args.username)
        if user is None:
            print(f"No user {args.username!r}.")
            return 1

        if args.rollback:
            return await rollback(user.id, args.dry_run)
        code, planned = await migrate(user.id, args.dry_run)
        await fill_merchant_cache(user.id, args.dry_run, planned)
        return code
    finally:
        await db_manager.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
