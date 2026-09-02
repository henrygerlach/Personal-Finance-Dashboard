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
import time
from dataclasses import dataclass
from typing import Sequence

import anthropic

from app.classification.pricing import CallRecord, UsageLedger
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
    code: str
    confidence: float
    #: Only asked for below the threshold, so it is empty for most results.
    reason: str = ""


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
        threshold: float | None = None,
        concurrency: int | None = None,
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
        self.concurrency = concurrency or settings.classification_concurrency
        # Built once: rebuilding per request would be wasteful, and a system
        # block that differs between requests would never cache.
        self.threshold = (
            threshold
            if threshold is not None
            else settings.classification_confidence_threshold
        )
        self._system_prompt = build_system_prompt(self.threshold)
        self._schema = build_response_schema()
        self.last_usage: dict | None = None
        #: Every call this classifier made, for the import report.
        self.ledger = UsageLedger()
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
        """
        The varying part: the merchants themselves, after the breakpoint.

        Ids are the position in this batch rather than the caller's own
        identifiers. They are short, which keeps the answer small, and they
        cannot leak a database id into the request.
        """
        payload = [
            {
                "id": index,
                "text": request.text,
                "amount": request.amount,
                "process": request.process or "",
            }
            for index, request in enumerate(batch, start=1)
        ]
        return (
            "Classify each of these transactions. Return one result per "
            "line, using the same ids.\n\n" + json.dumps(payload, ensure_ascii=False)
        )

    # --- Single batch -------------------------------------------------------

    async def _classify_batch_once(
        self, batch: Sequence[ClassificationRequest]
    ) -> list[ClassificationResult]:
        started = time.monotonic()
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=min(8000, 200 * len(batch) + 1000),
            system=self._system_blocks(),
            messages=[{"role": "user", "content": self._user_message(batch)}],
            output_config={"format": {"type": "json_schema", "schema": self._schema}},
        )

        usage = response.usage
        call = self.ledger.record(
            CallRecord(
                model=self.model,
                batch_size=len(batch),
                duration_seconds=time.monotonic() - started,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_input_tokens=getattr(
                    usage, "cache_creation_input_tokens", 0
                )
                or 0,
                cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", 0)
                or 0,
            )
        )
        self.last_usage = {
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "cache_creation_input_tokens": call.cache_creation_input_tokens,
            "cache_read_input_tokens": call.cache_read_input_tokens,
        }
        logger.info(
            "%s: %s merchants in %.2fs — in=%s cache_write=%s cache_read=%s "
            "out=%s cost=%s",
            self.model,
            call.batch_size,
            call.duration_seconds,
            call.input_tokens,
            call.cache_creation_input_tokens,
            call.cache_read_input_tokens,
            call.output_tokens,
            f"${call.cost:.5f}" if call.cost is not None else "unknown",
        )

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
        by_position = {index: request for index, request in enumerate(batch, start=1)}

        results = []
        for item in payload["results"]:
            request = by_position.get(int(item["id"]))
            if request is None:
                # An id nobody asked about. Dropping it is right: the caller
                # detects the transaction it did ask about as missing.
                logger.warning("Response carried unknown id %r", item["id"])
                continue
            results.append(
                ClassificationResult(
                    id=request.id,
                    code=str(item["c"]).strip(),
                    # The schema cannot express a range, so it is clamped here.
                    confidence=min(1.0, max(0.0, float(item["p"]))),
                    reason=str(item.get("r") or ""),
                )
            )
        return results

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
        batches = list(_batched(list(requests), self.batch_size))

        # The first batch goes alone, because it is the one that writes the
        # cached system block. Batches sent alongside it would each find no
        # cache to read and pay to write the same block again.
        for batch in batches[:1]:
            batch_results, failure = await self._classify_batch(batch)
            results.extend(batch_results)
            if failure is not None:
                failures.append(failure)

        # The rest read that cache and can overlap. Gathered rather than
        # awaited in turn: an import is a dozen requests and in sequence the
        # wait is the sum of them.
        limit = asyncio.Semaphore(max(1, self.concurrency))

        async def run(batch):
            async with limit:
                return await self._classify_batch(batch)

        for batch_results, failure in await asyncio.gather(
            *(run(batch) for batch in batches[1:])
        ):
            results.extend(batch_results)
            if failure is not None:
                failures.append(failure)

        # A batch can come back complete and still be missing lines: the
        # model answers the ones it is sure about and quietly drops the rest.
        # Those are asked about again on their own, where there is nothing
        # else in the request to distract from them.
        results, failures = await self._retry_missing(requests, results, failures)
        return results, failures

    async def _retry_missing(
        self,
        requests: Sequence[ClassificationRequest],
        results: list[ClassificationResult],
        failures: list[BatchFailure],
    ) -> tuple[list[ClassificationResult], list[BatchFailure]]:
        """Ask again, one at a time, about anything the model skipped."""
        covered = {result.id for result in results}
        covered |= {i for failure in failures for i in failure.ids}
        missing = [request for request in requests if request.id not in covered]
        if not missing:
            return results, failures

        logger.warning(
            "Classifier skipped %s of %s transactions; asking individually",
            len(missing),
            len(requests),
        )

        still_missing: list[str] = []
        for request in missing:
            retry_results, failure = await self._classify_batch([request])
            found = [result for result in retry_results if result.id == request.id]
            if found:
                results.extend(found)
            else:
                still_missing.append(request.id)

        if still_missing:
            failures.append(
                BatchFailure(tuple(still_missing), "no result after individual retry")
            )
        return results, failures

    async def count_prompt_tokens(self) -> int:
        """Token count of the cached system block, to check it can cache at all."""
        response = await self.client.messages.count_tokens(
            model=self.model,
            system=self._system_blocks(),
            messages=[{"role": "user", "content": "x"}],
        )
        return response.input_tokens
