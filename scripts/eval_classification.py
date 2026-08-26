#!/usr/bin/env python3
"""
Measure the classifier against the hand-made categories.

The ground truth is the 2,253 transactions whose category was assigned by hand
under the old rule system and then translated by the migration. Everything the
classifier has already touched is excluded: the four deferred buckets, the
PayPal fallback and anything already in review are not evidence about the
model, they are its own output.

Three things this deliberately does:

*The merchant cache is switched off.* It was built out of these same
transactions, so leaving it on would measure the cache and report it as model
accuracy.

*Few-shot overlap is reported.* The prompt contains worked examples taken from
this data. A sampled transaction whose merchant appears in an example has had
the answer handed to it, and the score is inflated by exactly that much.
``--exclude-fewshot`` removes them.

*The sample is seeded.* Two models can only be compared on the same
transactions, so the same seed must produce the same sample.

Usage:
    python scripts/eval_classification.py --dry-run
    python scripts/eval_classification.py --model claude-haiku-4-5
    python scripts/eval_classification.py --model claude-sonnet-5 --seed 42
"""

import argparse
import asyncio
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classification.client import (  # noqa: E402
    ClassificationError,
    ClaudeClassifier,
)
from app.classification.prompt import FEW_SHOT_EXAMPLES  # noqa: E402
from app.classification.service import ClassificationService  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import db_manager  # noqa: E402
from app.models import SOURCE_LEGACY_RULES, Transaction  # noqa: E402
from app.normalization import merchant_key, normalize_merchant  # noqa: E402

#: Merchants the prompt already answers. A sampled transaction matching one of
#: these has been told the answer, so it cannot be evidence about the model.
FEW_SHOT_KEYS = {merchant_key(example["text"]) for example in FEW_SHOT_EXAMPLES}


def stratified_sample(transactions, size: int, seed: int):
    """
    Take ``size`` transactions spread across the categories.

    A plain random sample would be almost entirely groceries — the largest
    category alone is a quarter of the data — and would say nothing about the
    long tail, which is where the classifier actually struggles. Each category
    gets a share of the sample proportional to its size, and every category
    present contributes at least one transaction.
    """
    rng = random.Random(seed)

    strata: dict[tuple[str, str], list] = defaultdict(list)
    for transaction in transactions:
        strata[(transaction.major_category, transaction.minor_category)].append(
            transaction
        )

    for group in strata.values():
        rng.shuffle(group)

    total = len(transactions)
    quotas = {
        pair: max(1, round(size * len(group) / total)) for pair, group in strata.items()
    }

    # Rounding up to a minimum of one overshoots; trim from the largest strata
    # so the rare categories keep their single representative.
    while sum(quotas.values()) > size:
        pair = max(quotas, key=lambda p: (quotas[p], len(strata[p])))
        if quotas[pair] <= 1:
            break
        quotas[pair] -= 1

    sample = []
    for pair, quota in quotas.items():
        sample.extend(strata[pair][: min(quota, len(strata[pair]))])
    rng.shuffle(sample)
    return sample


def confusion(rows, level: str, limit: int) -> list[tuple[str, str, int]]:
    """The most frequent confusions at one level, worst first."""
    counter: Counter = Counter()
    for row in rows:
        truth = row[f"true_{level}"]
        got = row[f"got_{level}"]
        if truth != got:
            counter[(truth, got)] += 1
    return [(a, b, n) for (a, b), n in counter.most_common(limit)]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--model", help="model to evaluate")
    parser.add_argument("--size", type=int, default=300)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--exclude-fewshot",
        action="store_true",
        help="drop sampled transactions whose merchant appears in the prompt",
    )
    parser.add_argument(
        "--no-escalation", action="store_true", help="skip the second pass"
    )
    parser.add_argument("--top", type=int, default=12, help="confusions to list")
    parser.add_argument("--out", help="write the per-transaction result as JSON")
    parser.add_argument(
        "--dry-run", action="store_true", help="describe the sample, call nothing"
    )
    args = parser.parse_args()

    model = args.model or settings.classification_model

    await db_manager.connect()
    try:
        population = await Transaction.find(
            Transaction.classification_source == SOURCE_LEGACY_RULES
        ).to_list()

        print(f"database             : {settings.database_name}")
        print(f"ground truth          : {len(population)} hand-categorised")
        if not population:
            print("Nothing to evaluate.")
            return 1

        sample = stratified_sample(population, args.size, args.seed)

        overlap = [
            t
            for t in sample
            if merchant_key(normalize_merchant(t.booking_text, t.process))
            in FEW_SHOT_KEYS
        ]
        if args.exclude_fewshot and overlap:
            excluded = {str(t.id) for t in overlap}
            sample = [t for t in sample if str(t.id) not in excluded]

        strata = len({(t.major_category, t.minor_category) for t in sample})
        print(f"sample                : {len(sample)} across {strata} categories")
        print(f"seed                  : {args.seed}")
        print(
            f"few-shot overlap      : {len(overlap)}"
            + (
                " (excluded)"
                if args.exclude_fewshot
                else " (kept — inflates the score)"
            )
        )
        print(f"model                 : {model}")

        if args.dry_run:
            print("\nDry run — no API calls.")
            return 0

        settings.classification_model = model
        try:
            classifier = ClaudeClassifier(model=model)
        except ClassificationError as error:
            print(f"\n{error}")
            return 2

        service = ClassificationService(
            classifier=classifier,
            escalation_classifier=(
                None
                if args.no_escalation
                else ClaudeClassifier(model=settings.classification_escalation_model)
            ),
            escalate=not args.no_escalation,
        )
        # The cache came out of these very transactions; leaving it on would
        # measure the cache and call the result model accuracy.
        run = await service.classify(sample, use_cache=False)
    finally:
        await db_manager.disconnect()

    by_id = {c.transaction_id: c for c in run.classifications}
    rows = []
    for transaction in sample:
        result = by_id.get(str(transaction.id))
        rows.append(
            {
                "id": str(transaction.id),
                "merchant": normalize_merchant(
                    transaction.booking_text, transaction.process
                ),
                "amount": transaction.amount,
                "true_major": transaction.major_category,
                "true_minor": transaction.minor_category,
                "got_major": result.major if result else None,
                "got_minor": result.minor if result else None,
                "confidence": result.confidence if result else None,
                "review": result.needs_review if result else True,
            }
        )

    answered = [row for row in rows if row["got_major"] is not None]
    decided = [row for row in answered if not row["review"]]
    major_hits = sum(1 for r in decided if r["true_major"] == r["got_major"])
    minor_hits = sum(1 for r in decided if r["true_minor"] == r["got_minor"])

    def pct(part: int, whole: int) -> str:
        return f"{part / whole * 100:5.1f}%" if whole else "    — "

    print()
    print("=" * 62)
    print(
        f"completeness          : {pct(len(answered), len(rows))} "
        f"({len(answered)}/{len(rows)} got a result at all)"
    )
    print(
        f"sent to review        : {pct(len(answered) - len(decided), len(rows))} "
        f"({len(answered) - len(decided)})"
    )
    print(f"decided               : {len(decided)}")
    print()
    print(
        f"major accuracy        : {pct(major_hits, len(decided))} "
        f"({major_hits}/{len(decided)})"
    )
    print(
        f"minor accuracy        : {pct(minor_hits, len(decided))} "
        f"({minor_hits}/{len(decided)})"
    )
    print()
    print("Accuracy counts only decided transactions. A transaction in review")
    print("is not a wrong answer — it is the classifier declining to guess, and")
    print("scoring it as a miss would reward overconfidence.")

    for level in ("major", "minor"):
        pairs = confusion(decided, level, args.top)
        if not pairs:
            continue
        print()
        print(f"--- most frequent {level} confusions ---")
        for truth, got, count in pairs:
            print(f"  {count:>4}  {truth}  ->  {got}")

    print()
    from app.classification.service import report_lines

    print("\n".join(report_lines(run, len(sample))))

    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1, ensure_ascii=False))
        print(f"\nper-transaction detail written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
