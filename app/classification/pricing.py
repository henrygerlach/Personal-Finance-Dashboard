"""
Model prices and the cost arithmetic for a classification run.

Without a number attached, none of the batching, deduplication or prompt
caching can be shown to work — a cache that silently stops hitting looks
exactly like one that hits, except on the invoice.

Prices are per million tokens, in US dollars, and they change. They live here
as one table with the date they were checked rather than scattered through the
code.
"""

from dataclasses import dataclass, field

#: Prices per million tokens, checked against the Anthropic pricing page on
#: 2026-06-24. Update the date when you update the numbers.
PRICES_CHECKED_ON = "2026-06-24"

MODEL_PRICES: dict[str, tuple[float, float]] = {
    # model id: (input $/MTok, output $/MTok)
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: Writing to the prompt cache costs a quarter more than an ordinary input
#: token; reading from it costs a tenth. These multipliers are what make the
#: cached prefix worth having, and what makes a silent cache miss expensive.
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

#: The Message Batches API halves every token price.
BATCH_API_MULTIPLIER = 0.50


def _canonical(model: str) -> str:
    """
    Strip a dated snapshot suffix so both spellings of a model resolve.

    "claude-haiku-4-5-20251001" and "claude-haiku-4-5" are the same model and
    must not price differently depending on which form a config happens to
    carry.
    """
    parts = model.split("-")
    if parts and parts[-1].isdigit() and len(parts[-1]) == 8:
        return "-".join(parts[:-1])
    return model


def price_for(model: str) -> tuple[float, float] | None:
    """Input and output price per million tokens, or ``None`` if unknown."""
    return MODEL_PRICES.get(_canonical(model))


@dataclass
class CallRecord:
    """What one API request cost and how it was served."""

    model: str
    batch_size: int
    duration_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def cost(self) -> float | None:
        """
        Estimated cost in dollars, or ``None`` for a model with no price.

        ``input_tokens`` excludes what was served from or written to the
        cache; the API reports those separately, and they are billed at
        different rates.
        """
        prices = price_for(self.model)
        if prices is None:
            return None
        input_price, output_price = prices
        return (
            self.input_tokens * input_price
            + self.cache_creation_input_tokens * input_price * CACHE_WRITE_MULTIPLIER
            + self.cache_read_input_tokens * input_price * CACHE_READ_MULTIPLIER
            + self.output_tokens * output_price
        ) / 1_000_000


@dataclass
class UsageLedger:
    """Every call of a run, and the totals over them."""

    calls: list[CallRecord] = field(default_factory=list)

    def record(self, call: CallRecord) -> CallRecord:
        self.calls.append(call)
        return call

    @property
    def total_cost(self) -> float | None:
        costs = [call.cost for call in self.calls]
        if not costs or any(cost is None for cost in costs):
            return None
        return sum(costs)

    @property
    def cached_input_tokens(self) -> int:
        return sum(call.cache_read_input_tokens for call in self.calls)

    @property
    def billable_input_tokens(self) -> int:
        """Input tokens that were not served from the cache."""
        return sum(
            call.input_tokens + call.cache_creation_input_tokens for call in self.calls
        )

    @property
    def prompt_cache_hit_rate(self) -> float:
        """
        The share of input tokens served from the prompt cache.

        Zero across a whole run with a cache breakpoint set means the prefix
        is below the model's minimum cacheable length. That fails silently:
        no error, no cache, full price.
        """
        total = self.cached_input_tokens + self.billable_input_tokens
        return self.cached_input_tokens / total if total else 0.0

    @property
    def output_tokens(self) -> int:
        return sum(call.output_tokens for call in self.calls)

    @property
    def duration_seconds(self) -> float:
        return sum(call.duration_seconds for call in self.calls)

    def summary_lines(self) -> list[str]:
        """A readable block for an import report."""
        unpriced = {call.model for call in self.calls if price_for(call.model) is None}
        cost = self.total_cost
        lines = [
            f"api calls            : {len(self.calls)} in {self.duration_seconds:.1f}s",
            f"input tokens         : {self.billable_input_tokens} billed, "
            f"{self.cached_input_tokens} from cache "
            f"({self.prompt_cache_hit_rate * 100:.0f}%)",
            f"output tokens        : {self.output_tokens}",
        ]
        if cost is None:
            lines.append(
                f"estimated cost       : unknown, no price for "
                f"{', '.join(sorted(unpriced))}"
            )
        else:
            lines.append(
                f"estimated cost       : ${cost:.4f} (prices of {PRICES_CHECKED_ON})"
            )
        return lines
