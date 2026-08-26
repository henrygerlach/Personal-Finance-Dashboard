"""
Tests for the rules-versus-classifier comparison log.

The log is the only evidence for deciding the rule engine can be switched off,
so it has to be readable and, above all, honest about disagreement.
"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.classification.comparison import _translate_legacy, write_comparison


def transaction(tid, major, minor, amount=-10.0, text="EDEKA BOECKER"):
    return SimpleNamespace(
        id=tid,
        date=datetime(2026, 8, 26),
        amount=amount,
        booking_text=text,
        process=None,
        major_category=major,
        minor_category=minor,
    )


def classification(tid, major, minor, confidence=0.9, source="llm"):
    from app.categories import flow_type_for
    from app.classification.service import Classification

    return Classification(
        transaction_id=str(tid),
        major=major,
        minor=minor,
        flow_type=flow_type_for(major),
        confidence=confidence,
        reason="",
        source=source,
        merchant="EDEKA BOECKER",
    )


def test_the_rule_verdict_is_translated_before_comparing(tmp_path):
    """
    The two systems speak different taxonomies. Comparing them raw would
    report every single transaction as a disagreement and say nothing.
    """
    assert _translate_legacy("Food & Drinks", "Groceries") == (
        "Food & Drink",
        "Groceries",
    )


def test_an_unmapped_legacy_pair_is_shown_rather_than_guessed():
    assert _translate_legacy("Invented", "Nonsense") == ("Invented", "Nonsense")


def test_agreement_and_disagreement_are_counted(tmp_path):
    log = tmp_path / "comparison.log"
    transactions = [
        transaction("1", "Food & Drinks", "Groceries"),
        transaction("2", "Lifestyle", "Clothing"),
    ]
    classifications = [
        classification("1", "Food & Drink", "Groceries"),
        classification("2", "Shopping", "Electronics & Tech"),
    ]

    path, agreed, differed = write_comparison(transactions, classifications, str(log))

    assert (agreed, differed) == (1, 1)
    body = path.read_text()
    assert "SAME " in body and "DIFF " in body
    assert "agreed 1, differed 1 (50%)" in body


def test_disagreements_can_be_grepped_out(tmp_path):
    """A run of a few hundred lines is only useful if the differences find."""
    log = tmp_path / "comparison.log"
    write_comparison(
        [transaction("1", "Lifestyle", "Clothing")],
        [classification("1", "Shopping", "Electronics & Tech")],
        str(log),
    )
    diffs = [line for line in log.read_text().splitlines() if line.startswith("DIFF")]
    assert len(diffs) == 1
    assert "rules: Shopping / Clothing & Shoes" in diffs[0]
    assert "model: Shopping / Electronics & Tech" in diffs[0]


def test_the_log_records_the_flow_type_and_the_source(tmp_path):
    log = tmp_path / "comparison.log"
    write_comparison(
        [transaction("1", "Transfers", "Internal", amount=-500.0)],
        [classification("1", "Transfers & Savings", "Internal Transfer", 1.0, "user")],
        str(log),
    )
    line = [line for line in log.read_text().splitlines() if line.startswith("SAME")][0]
    assert "flow: transfer" in line
    assert "p=1.00, user" in line


def test_appending_a_second_import_keeps_the_first(tmp_path):
    """Each import is a section; the file is a history, not a snapshot."""
    log = tmp_path / "comparison.log"
    for _ in range(2):
        write_comparison(
            [transaction("1", "Food & Drinks", "Groceries")],
            [classification("1", "Food & Drink", "Groceries")],
            str(log),
        )
    assert log.read_text().count("=== import") == 2


def test_a_transaction_without_a_classification_is_skipped(tmp_path):
    """A failed batch must not put a half-written line in the log."""
    log = tmp_path / "comparison.log"
    _, agreed, differed = write_comparison(
        [transaction("1", "Food & Drinks", "Groceries")], [], str(log)
    )
    assert (agreed, differed) == (0, 0)
    assert "SAME" not in log.read_text()


@pytest.mark.parametrize("count", [0, 1])
def test_the_summary_line_survives_an_empty_import(tmp_path, count):
    log = tmp_path / "comparison.log"
    transactions = [transaction("1", "Food & Drinks", "Groceries")][:count]
    classifications = [classification("1", "Food & Drink", "Groceries")][:count]
    write_comparison(transactions, classifications, str(log))
    assert "agreed" in log.read_text()
