"""
Log the rule engine and the classifier side by side during an import.

The rule system stays in place while the classifier is being trusted, and the
only way to know whether it can be switched off is to see where the two
disagree on real imports. Every transaction is written to a log file with both
verdicts; agreements are one line, disagreements are marked so they can be
grepped out.

This writes to a file rather than the database on purpose: it is a record of
one import, read once and then thrown away, and it must never be mistaken for
the transaction's category.
"""

from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from app.categories import flow_type_for
from app.config import get_logger, settings
from app.legacy_mapping import map_pair

logger = get_logger(__name__)


def _translate_legacy(major: str | None, minor: str | None) -> tuple[str, str]:
    """
    Put the rule engine's verdict into the new taxonomy so the two are
    comparable at all. An unmapped pair is reported as-is rather than guessed.
    """
    try:
        return map_pair(major or "Other", minor or "Uncategorized")[0]
    except KeyError:
        return (major or "?", minor or "?")


def write_comparison(
    transactions: Sequence,
    classifications: Iterable,
    path: str | None = None,
) -> tuple[Path, int, int]:
    """
    Append one import's comparison to the log.

    Args:
        transactions: The imported transactions, still carrying the rule
            engine's categories in ``major_category`` / ``minor_category``.
        classifications: What the classifier decided for them.
        path: Override the configured log file.

    Returns:
        The file written to, how many transactions agreed, and how many did
        not.
    """
    target = Path(path or settings.comparison_log_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    by_id = {c.transaction_id: c for c in classifications}
    agreed = disagreed = 0
    lines = [
        f"=== import {datetime.utcnow().isoformat(timespec='seconds')}Z "
        f"— {len(transactions)} transactions ==="
    ]

    for transaction in transactions:
        classification = by_id.get(str(transaction.id))
        if classification is None:
            continue

        rules_pair = _translate_legacy(
            transaction.major_category, transaction.minor_category
        )
        model_pair = (classification.major, classification.minor)
        same = rules_pair == model_pair
        agreed += same
        disagreed += not same

        lines.append(
            f"{'SAME ' if same else 'DIFF '}"
            f"{transaction.date:%Y-%m-%d} {transaction.amount:>10.2f} "
            f"| rules: {rules_pair[0]} / {rules_pair[1]}"
            f" | model: {model_pair[0]} / {model_pair[1]}"
            f" (p={classification.confidence:.2f}, {classification.source})"
            f" | flow: {flow_type_for(model_pair[0])}"
            f" | {(transaction.booking_text or '')[:70]}"
        )

    total = agreed + disagreed
    share = f"{disagreed / total * 100:.0f}%" if total else "—"
    lines.append(f"--- agreed {agreed}, differed {disagreed} ({share}) ---")

    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    logger.info(
        "Comparison written to %s: %s agreed, %s differed",
        target,
        agreed,
        disagreed,
    )
    return target, agreed, disagreed
