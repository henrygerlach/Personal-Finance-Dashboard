"""
The Claude API client for transaction classification.

Three things shape this module.

*Batching.* One request classifies many transactions, so the cached system
prompt is paid for once per batch instead of once per transaction.

*Prompt caching.* The system block is constant and carries a cache breakpoint;
everything that varies goes after it. ``last_usage`` exposes what the API
reported so a caller can check the cache is actually being hit — a silent miss
looks exactly like a hit apart from the bill.

*Isolation.* A batch that fails after its retries returns failures for its own
transactions and nothing else. One bad batch must never take an import down.
"""

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Sequence

import anthropic

from app.classification.prompt import build_response_schema, build_system_prompt
from app.config import get_logger, settings

logger = get_logger(__name__)


@dataclass(frozen=True)
class ClassificationRequest:
    """One transaction as the classifier sees it."""

    id: str
    text: str
    amount: float
    process: str | None = None


@dataclass(frozen=True)
class ClassificationResult:
    """What the model returned for one transaction, before validation."""

    id: str
    major: str
    minor: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class BatchFailure:
    """A batch the API would not answer, and the transactions it covered."""

    ids: tuple[str, ...]
    error: str


class ClassificationError(RuntimeError):
    """Raised when the classifier cannot be used at all, such as a missing key."""


def _batched(items: Sequence, size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


class ClaudeClassifier:
    """Classifies batches of transactions through the Messages API."""

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        model: str | None = None,
        batch_size: int | None = None,
        max_attempts: int | None = None,
    ) -> None:
        if client is None:
            if not settings.anthropic_api_key:
                raise ClassificationError(
                    "ANTHROPIC_API_KEY is not set; classification is unavailable"
                )
            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.client = client
        self.model = model or settings.classification_model
        self.batch_size = batch_size or settings.classification_batch_size
        self.max_attempts = max_attempts or settings.classification_max_attempts
        # Built once: rebuilding per request would be wasteful, and a system
        # block that differs between requests would never cache.
        self._system_prompt = build_system_prompt()
        self._schema = build_response_schema()
        self.last_usage: dict | None = None
        # Checked once per classifier, not per batch, to keep the log quiet.
        self._cache_checked = False

    # --- Request shaping ----------------------------------------------------

    def _system_blocks(self) -> list[dict]:
        """The system block with a cache breakpoint after it."""
        return [
            {
                "type": "text",
                "text": self._system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @staticmethod
    def _user_message(batch: Sequence[ClassificationRequest]) -> str:
        """The varying part: the transactions themselves, after the breakpoint."""
        payload = [
            {
                "id": request.id,
                "text": request.text,
                "amount": request.amount,
                "process": request.process or "",
            }
            for request in batch
        ]
        return (
            "Classify each of these transactions. Return one result per "
            "transaction, keeping the ids exactly as given.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1)
        )

    # --- Single batch -------------------------------------------------------

    async def _classify_batch_once(
        self, batch: Sequence[ClassificationRequest]
    ) -> list[ClassificationResult]:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=min(8000, 200 * len(batch) + 1000),
            system=self._system_blocks(),
            messages=[{"role": "user", "content": self._user_message(batch)}],
            output_config={"format": {"type": "json_schema", "schema": self._schema}},
        )

        usage = response.usage
        self.last_usage = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": getattr(
                usage, "cache_creation_input_tokens", 0
            ),
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
        }

        if not self._cache_checked:
            self._cache_checked = True
            if not (
                self.last_usage["cache_creation_input_tokens"]
                or self.last_usage["cache_read_input_tokens"]
            ):
                # Caching fails silently: too short a prefix is not an error,
                # it just never caches, and only the bill would show it. The
                # minimum is model-dependent and not monotonic across
                # generations — 4096 tokens on Haiku 4.5, far less on the
                # Sonnet and Opus tiers.
                logger.warning(
                    "Prompt caching did not engage for %s: the %s-token system "
                    "block is below this model's minimum cacheable prefix. "
                    "Requests still work, they just cost full price.",
                    self.model,
                    usage.input_tokens,
                )

        text = next(
            (block.text for block in response.content if block.type == "text"), ""
        )
        payload = json.loads(text)
        return [
            ClassificationResult(
                id=str(item["id"]),
                major=item["major"],
                minor=item["minor"],
                # The schema cannot express a range, so it is clamped here.
                confidence=min(1.0, max(0.0, float(item["confidence"]))),
                reason=str(item.get("reason", "")),
            )
            for item in payload["results"]
        ]

    async def _classify_batch(
        self, batch: Sequence[ClassificationRequest]
    ) -> tuple[list[ClassificationResult], BatchFailure | None]:
        """Classify one batch, retrying transient failures with backoff."""
        ids = tuple(request.id for request in batch)
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                return await self._classify_batch_once(batch), None
            except (
                anthropic.RateLimitError,
                anthropic.APITimeoutError,
                anthropic.APIConnectionError,
            ) as error:
                last_error = error
            except anthropic.APIStatusError as error:
                if error.status_code < 500:
                    # A malformed request will fail the same way every time.
                    logger.error("Batch rejected (%s): %s", error.status_code, error)
                    return [], BatchFailure(ids, f"{error.status_code}: {error}")
                last_error = error
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                # A response that does not fit the schema is worth one more
                # try; the same prompt usually parses on the next attempt.
                last_error = error

            if attempt < self.max_attempts:
                delay = min(30.0, 2**attempt) + random.uniform(0, 1)
                logger.warning(
                    "Batch attempt %s/%s failed (%s); retrying in %.1fs",
                    attempt,
                    self.max_attempts,
                    last_error,
                    delay,
                )
                await asyncio.sleep(delay)

        logger.error(
            "Batch gave up after %s attempts: %s", self.max_attempts, last_error
        )
        return [], BatchFailure(ids, str(last_error))

    # --- Public API ---------------------------------------------------------

    async def classify(
        self, requests: Sequence[ClassificationRequest]
    ) -> tuple[list[ClassificationResult], list[BatchFailure]]:
        """
        Classify every request, in batches.

        Returns:
            The results that came back and the batches that did not. A failed
            batch yields a ``BatchFailure`` naming its transactions rather
            than raising, so the caller can file those for review and keep the
            rest of the run.
        """
        results: list[ClassificationResult] = []
        failures: list[BatchFailure] = []

        for batch in _batched(list(requests), self.batch_size):
            batch_results, failure = await self._classify_batch(batch)
            results.extend(batch_results)
            if failure is not None:
                failures.append(failure)

        returned = {result.id for result in results}
        covered = returned | {i for failure in failures for i in failure.ids}
        missing = [request.id for request in requests if request.id not in covered]
        if missing:
            # The model answered the batch but skipped transactions in it.
            logger.warning("Classifier returned no result for %s ids", len(missing))
            failures.append(BatchFailure(tuple(missing), "no result returned"))

        return results, failures

    async def count_prompt_tokens(self) -> int:
        """Token count of the cached system block, to check it can cache at all."""
        response = await self.client.messages.count_tokens(
            model=self.model,
            system=self._system_blocks(),
            messages=[{"role": "user", "content": "x"}],
        )
        return response.input_tokens
