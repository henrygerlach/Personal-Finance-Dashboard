"""
Tests for the cost arithmetic.

The numbers exist so that batching, deduplication and prompt caching can be
shown to work rather than assumed to. A cache that quietly stops hitting looks
exactly like one that hits, except on the invoice.
"""

import pytest

from app.classification.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    MODEL_PRICES,
    PRICES_CHECKED_ON,
    CallRecord,
    UsageLedger,
    price_for,
)


def test_a_dated_model_id_prices_the_same_as_the_plain_one():
    """
    Both spellings name one model. Pricing them differently depending on
    which form a config happens to carry would be a silent reporting error.
    """
    assert price_for("claude-haiku-4-5-20251001") == price_for("claude-haiku-4-5")
    assert price_for("claude-sonnet-5") == (2.00, 10.00)


def test_an_unknown_model_has_no_price_rather_than_a_wrong_one():
    assert price_for("something-else") is None
    call = CallRecord("something-else", 25, 1.0, input_tokens=1000)
    assert call.cost is None


def test_cost_uses_the_three_input_rates_separately():
    """
    Cached reads are a tenth and cache writes a quarter more than an ordinary
    input token. Collapsing them into one rate is how a cache miss becomes
    invisible.
    """
    call = CallRecord(
        "claude-haiku-4-5",
        batch_size=25,
        duration_seconds=1.0,
        input_tokens=1_000_000,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    expected = 1.0 + 1.0 * CACHE_WRITE_MULTIPLIER + 1.0 * CACHE_READ_MULTIPLIER + 5.0
    assert call.cost == pytest.approx(expected)


def test_a_ledger_without_a_priced_call_reports_no_total():
    ledger = UsageLedger()
    ledger.record(CallRecord("claude-haiku-4-5", 1, 1.0, input_tokens=1000))
    ledger.record(CallRecord("mystery-model", 1, 1.0, input_tokens=1000))
    assert ledger.total_cost is None
    assert "unknown" in "\n".join(ledger.summary_lines())


def test_prompt_cache_hit_rate_is_zero_when_nothing_cached():
    """
    This is the number that exposes a prefix below the model's minimum
    cacheable length, which otherwise fails without any error at all.
    """
    ledger = UsageLedger()
    ledger.record(CallRecord("claude-haiku-4-5", 25, 1.0, input_tokens=3600))
    assert ledger.prompt_cache_hit_rate == 0.0


def test_prompt_cache_hit_rate_counts_reads_against_all_input():
    ledger = UsageLedger()
    ledger.record(
        CallRecord(
            "claude-sonnet-5",
            25,
            1.0,
            input_tokens=250,
            cache_read_input_tokens=750,
        )
    )
    assert ledger.prompt_cache_hit_rate == pytest.approx(0.75)


def test_an_empty_ledger_does_not_divide_by_zero():
    ledger = UsageLedger()
    assert ledger.prompt_cache_hit_rate == 0.0
    assert ledger.total_cost is None


def test_every_price_is_plausible_and_dated():
    """
    A typo in the table would misreport every run. Output always costs more
    than input, and the date must say when the numbers were checked.
    """
    assert PRICES_CHECKED_ON
    for model, (input_price, output_price) in MODEL_PRICES.items():
        assert 0 < input_price < 100, model
        assert output_price > input_price, model
