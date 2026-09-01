"""
Tests for the Claude API client.

Nothing here talks to the API; a stand-in records what it was asked and
returns what the test wants back. What is defended is the three things the
module exists to get right — batching, the cached prefix, and isolation of a
failure — plus the two silent failure modes that motivated it. A prompt cache
that stops engaging looks exactly like one that engages, apart from the bill.
And a model that answers eleven of twelve merchants returns a perfectly valid
response; the twelfth simply is not in it, and nothing raises.
"""

import asyncio
import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.classification.client import (
    ClassificationError,
    ClassificationRequest,
    ClaudeClassifier,
    _batched,
)


def request(id="1", text="AMAZON", amount=-20.71, process=None):
    return ClassificationRequest(id=id, text=text, amount=amount, process=process)


def answer(*pairs, unknown_ids=()):
    """A response body answering positional id -> code."""
    results = [
        {"id": index, "c": code, "p": confidence}
        for index, (code, confidence) in enumerate(pairs, start=1)
    ]
    results.extend({"id": i, "c": "FOO", "p": 0.9} for i in unknown_ids)
    return json.dumps({"results": results})


def response(text, **usage):
    """One Messages API response, with the usage fields the ledger reads."""
    defaults = dict(
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=5000,
    )
    defaults.update(usage)
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(**defaults),
    )


class FakeMessages:
    """Records every request and replays a queue of scripted outcomes."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else self.last
        self.last = outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def count_tokens(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(input_tokens=4321)


def classifier(*outcomes, **kwargs):
    """A classifier wired to a fake API, never sleeping between retries."""
    client = SimpleNamespace(messages=FakeMessages(outcomes))
    kwargs.setdefault("model", "claude-haiku-4-5")
    kwargs.setdefault("batch_size", 25)
    kwargs.setdefault("max_attempts", 3)
    kwargs.setdefault("threshold", 0.7)
    return ClaudeClassifier(client=client, **kwargs)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """
    Retries back off for up to thirty seconds, which is right in production
    and would make this suite unrunnable.
    """

    async def instantly(_seconds):
        return None

    monkeypatch.setattr("app.classification.client.asyncio.sleep", instantly)


def run(coro):
    return asyncio.run(coro)


# --- Construction -------------------------------------------------------------


def test_a_missing_api_key_is_refused_at_construction(monkeypatch):
    """
    Better to fail before the import starts than to fail per batch after it
    has half-written a run. The message names the variable, because that is
    the only thing the person reading it has to fix.
    """
    monkeypatch.setattr("app.classification.client.settings.anthropic_api_key", None)

    with pytest.raises(ClassificationError, match="ANTHROPIC_API_KEY"):
        ClaudeClassifier()


def test_an_injected_client_needs_no_key(monkeypatch):
    """The evaluation script and these tests supply their own client."""
    monkeypatch.setattr("app.classification.client.settings.anthropic_api_key", None)

    assert ClaudeClassifier(client=SimpleNamespace(messages=None)) is not None


# --- Batching -----------------------------------------------------------------


def test_batching_covers_every_item_exactly_once():
    """
    A dropped or duplicated item is a transaction never classified or billed
    for twice, and neither shows up as an error anywhere.
    """
    items = list(range(10))

    assert [list(b) for b in _batched(items, 3)] == [
        [0, 1, 2],
        [3, 4, 5],
        [6, 7, 8],
        [9],
    ]


def test_a_run_larger_than_the_batch_size_becomes_several_requests():
    """
    The batch size is the lever that trades a cheaper cached prefix per
    transaction against how much one failure costs to retry.
    """
    subject = classifier(
        response(answer(("FOO1", 0.9), ("FOO2", 0.9))),
        response(answer(("FOO3", 0.9))),
        batch_size=2,
    )
    requests = [request(id=str(i), text=f"M{i}") for i in range(1, 4)]

    results, failures = run(subject.classify(requests))

    assert len(subject.client.messages.calls) == 2
    assert failures == []
    assert {r.id for r in results} == {"1", "2", "3"}


def test_one_request_carries_every_merchant_in_the_batch():
    """
    The whole point of batching is that the cached system block is paid for
    once per request rather than once per merchant.
    """
    subject = classifier(response(answer(("FOO1", 0.9), ("FOO2", 0.9))))

    run(subject.classify([request(id="a", text="ALDI"), request(id="b", text="LIDL")]))

    payload = subject.client.messages.calls[0]["messages"][0]["content"]
    assert "ALDI" in payload and "LIDL" in payload


# --- The cached prefix --------------------------------------------------------


def test_the_system_block_carries_a_cache_breakpoint():
    """
    Without the breakpoint the prefix is re-read at full price on every batch,
    and nothing else in the system would notice.
    """
    subject = classifier(response(answer(("FOO", 0.9))))

    run(subject.classify([request()]))

    blocks = subject.client.messages.calls[0]["system"]
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_the_system_block_is_identical_between_batches():
    """
    A prefix that differs by so much as a timestamp never hits the cache. This
    is the test that would fail if somebody put the batch size, the date or a
    merchant name into the system block.
    """
    subject = classifier(
        response(answer(("FOO", 0.9))), response(answer(("FOO", 0.9))), batch_size=1
    )

    run(subject.classify([request(id="a"), request(id="b")]))

    first, second = subject.client.messages.calls
    assert first["system"] == second["system"]


def test_the_merchants_go_after_the_breakpoint_not_into_it():
    """The varying part must sit in the user message or it defeats the cache."""
    subject = classifier(response(answer(("FOO", 0.9))))

    run(subject.classify([request(text="NYA HERING SANIKONZEPT")]))

    call = subject.client.messages.calls[0]
    assert "NYA HERING" not in call["system"][0]["text"]
    assert "NYA HERING" in call["messages"][0]["content"]


def test_a_cache_that_never_engages_is_warned_about_once(caplog):
    """
    Too short a prefix is not an API error — it simply never caches, and only
    the invoice would ever say so. The warning is the only signal there is.
    """
    subject = classifier(
        response(
            answer(("FOO", 0.9)),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        response(
            answer(("FOO", 0.9)),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        batch_size=1,
    )

    with caplog.at_level("WARNING"):
        run(subject.classify([request(id="a"), request(id="b")]))

    warnings = [
        r for r in caplog.records if "Prompt caching did not engage" in r.message
    ]
    assert len(warnings) == 1


def test_a_cache_that_engages_is_not_warned_about(caplog):
    """The check must stay quiet in the normal case or it will be ignored."""
    subject = classifier(response(answer(("FOO", 0.9)), cache_read_input_tokens=5000))

    with caplog.at_level("WARNING"):
        run(subject.classify([request()]))

    assert not [r for r in caplog.records if "did not engage" in r.message]


def test_what_the_api_reported_is_exposed_for_inspection():
    """
    ``last_usage`` is how a caller checks the cache is really being hit rather
    than assuming it from the configuration.
    """
    subject = classifier(
        response(
            answer(("FOO", 0.9)),
            input_tokens=120,
            output_tokens=30,
            cache_creation_input_tokens=4096,
            cache_read_input_tokens=0,
        )
    )

    run(subject.classify([request()]))

    assert subject.last_usage == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_creation_input_tokens": 4096,
        "cache_read_input_tokens": 0,
    }


def test_every_call_is_recorded_in_the_ledger():
    """The import report states what a run cost, and it reads this."""
    subject = classifier(
        response(answer(("FOO", 0.9))), response(answer(("FOO", 0.9))), batch_size=1
    )

    run(subject.classify([request(id="a"), request(id="b")]))

    assert len(subject.ledger.calls) == 2


# --- Reading the answer -------------------------------------------------------


def test_a_result_is_matched_back_to_the_caller_s_own_id():
    """
    The request numbers merchants by position so no database id leaves the
    process. The mapping back has to be exact — off by one files every
    merchant under its neighbour's category.
    """
    subject = classifier(response(answer(("FOOD_GROC", 0.9), ("TRAN_FUEL", 0.8))))

    results, _ = run(
        subject.classify(
            [request(id="alpha", text="ALDI"), request(id="beta", text="ARAL")]
        )
    )

    by_id = {r.id: r.code for r in results}
    assert by_id == {"alpha": "FOOD_GROC", "beta": "TRAN_FUEL"}


def test_no_database_id_is_ever_put_into_the_request():
    """
    Ids in the payload are positions. This is the test that fails if somebody
    "simplifies" it by sending the caller's id straight through.
    """
    subject = classifier(response(answer(("FOO", 0.9))))

    run(subject.classify([request(id="68af1e2c9b4d5a6f7c8d9e01")]))

    assert "68af1e2c" not in subject.client.messages.calls[0]["messages"][0]["content"]


def test_a_confidence_outside_zero_to_one_is_clamped():
    """
    The response schema cannot express a range, so a model returning 1.4 is a
    valid response. Left alone it outranks a hand-made correction.
    """
    subject = classifier(
        response(
            json.dumps(
                {
                    "results": [
                        {"id": 1, "c": "FOO", "p": 1.4},
                        {"id": 2, "c": "BAR", "p": -0.3},
                    ]
                }
            )
        )
    )

    results, _ = run(subject.classify([request(id="a"), request(id="b")]))

    assert sorted(r.confidence for r in results) == [0.0, 1.0]


def test_an_id_nobody_asked_about_is_dropped():
    """
    A hallucinated id must not become a classification. Dropping it is safe:
    the transaction that was asked about is then missing, and the individual
    retry picks it up.
    """
    subject = classifier(
        response(answer(("FOO", 0.9), unknown_ids=[99])),
        response(answer(("FOO", 0.9))),
    )

    results, _ = run(subject.classify([request(id="a")]))

    assert [r.id for r in results] == ["a"]


def test_the_reason_is_carried_through_when_the_model_gives_one():
    """
    Reasons are only asked for below the threshold, and they are what the
    review queue shows the person deciding.
    """
    subject = classifier(
        response(
            json.dumps(
                {"results": [{"id": 1, "c": "FOO", "p": 0.4, "r": "Name only."}]}
            )
        )
    )

    results, _ = run(subject.classify([request()]))

    assert results[0].reason == "Name only."


def test_a_missing_reason_becomes_an_empty_string_not_none():
    """Most results have none, and the caller stores the field unconditionally."""
    subject = classifier(response(answer(("FOO", 0.95))))

    results, _ = run(subject.classify([request()]))

    assert results[0].reason == ""


# --- When the model skips a merchant -----------------------------------------


def test_a_merchant_the_model_skipped_is_asked_about_alone():
    """
    A batch can return successfully and still be short: the model answers what
    it is sure about and quietly omits the rest. Nothing raises, so the only
    way to find out is to compare what came back against what was asked.
    """
    subject = classifier(
        response(answer(("FOO", 0.9))),
        response(answer(("BAR", 0.8))),
    )

    results, failures = run(
        subject.classify([request(id="a", text="ALDI"), request(id="b", text="ODD")])
    )

    assert failures == []
    assert {r.id for r in results} == {"a", "b"}
    assert len(subject.client.messages.calls) == 2


def test_a_merchant_still_unanswered_after_its_own_request_becomes_a_failure():
    """
    It has to end somewhere, and the caller needs the id so the transaction
    can go to review rather than vanish from the run.
    """
    subject = classifier(
        response(answer(("FOO", 0.9))),
        response(json.dumps({"results": []})),
    )

    _, failures = run(subject.classify([request(id="a"), request(id="b")]))

    assert failures[0].ids == ("b",)
    assert "individual retry" in failures[0].error


def test_nothing_is_retried_when_the_batch_was_complete():
    """The extra pass costs a request, so it must only happen when it is needed."""
    subject = classifier(response(answer(("FOO", 0.9), ("BAR", 0.9))))

    run(subject.classify([request(id="a"), request(id="b")]))

    assert len(subject.client.messages.calls) == 1


# --- Failure and retry --------------------------------------------------------


def _api_error(cls, code):
    """
    One API exception as the SDK raises it.

    The SDK reads the status code and the headers off a real response object,
    so a stand-in will not do here.
    """
    http_response = httpx2.Response(
        status_code=code, request=httpx2.Request("POST", "https://api.anthropic.com")
    )
    return cls("boom", response=http_response, body=None)


def _status_error(code):
    return _api_error(anthropic.APIStatusError, code)


def test_a_rate_limit_is_retried_and_then_succeeds():
    """Transient failures are the normal weather of a long import."""
    subject = classifier(
        _api_error(anthropic.RateLimitError, 429),
        response(answer(("FOO", 0.9))),
    )

    results, failures = run(subject.classify([request()]))

    assert failures == []
    assert len(results) == 1


def test_a_client_error_is_not_retried():
    """
    A malformed request fails identically every time. Retrying it three times
    turns one wasted call into three and delays the report of a real bug.
    """
    subject = classifier(_status_error(400))

    results, failures = run(subject.classify([request(id="a")]))

    assert results == []
    assert failures[0].ids == ("a",)
    assert len(subject.client.messages.calls) == 1


def test_a_server_error_is_retried_up_to_the_attempt_limit():
    """Five hundreds are transient; giving up on the first is giving up early."""
    subject = classifier(_status_error(500), _status_error(500), _status_error(500))

    _, failures = run(subject.classify([request(id="a")]))

    assert len(subject.client.messages.calls) == 3
    assert failures[0].ids == ("a",)


def test_an_unparseable_response_is_retried():
    """
    A response that does not fit the schema usually parses on the next
    attempt, so it is worth one more try before the batch is given up on.
    """
    subject = classifier(response("not json at all"), response(answer(("FOO", 0.9))))

    results, failures = run(subject.classify([request()]))

    assert failures == []
    assert len(results) == 1


def test_one_failing_batch_does_not_take_the_others_down():
    """
    This is the isolation rule the module was written around: a monthly import
    of a hundred merchants must not be lost to one bad batch.
    """
    subject = classifier(
        response(answer(("FOO", 0.9))),
        _status_error(400),
        response(answer(("BAZ", 0.9))),
        batch_size=1,
    )

    results, failures = run(
        subject.classify([request(id="a"), request(id="b"), request(id="c")])
    )

    assert {r.id for r in results} == {"a", "c"}
    assert failures[0].ids == ("b",)


def test_a_failed_batch_names_every_transaction_it_covered():
    """
    The caller files exactly these for review. A failure naming fewer than it
    covered leaves transactions in no state at all.
    """
    subject = classifier(_status_error(400), batch_size=3)

    _, failures = run(
        subject.classify([request(id="a"), request(id="b"), request(id="c")])
    )

    assert failures[0].ids == ("a", "b", "c")


def test_classifying_nothing_calls_no_api():
    """An import where the cache answered everything must cost nothing."""
    subject = classifier()

    results, failures = run(subject.classify([]))

    assert (results, failures) == ([], [])
    assert subject.client.messages.calls == []


# --- The prefix size check ----------------------------------------------------


def test_the_system_block_can_be_counted_without_classifying_anything():
    """
    Whether the prefix clears a model's minimum cacheable length is a question
    worth answering before a run rather than after the invoice.
    """
    subject = classifier()

    assert run(subject.count_prompt_tokens()) == 4321
