from typing import Any

import pytest
from botocore.exceptions import ClientError
from docpipe_core.llm import SummarizationError, SummarizerClient


def converse_response(text: str) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
        "stopReason": "end_turn",
    }


class FakeBedrock:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result  # type: ignore[no-any-return]


def throttle() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
    )


def test_summarize_happy_path() -> None:
    fake = FakeBedrock([converse_response("A concise summary.")])
    client = SummarizerClient(bedrock_client=fake)

    result = client.summarize("some long document text")

    assert result.summary == "A concise summary."
    assert result.model_id == "us.deepseek.r1-v1:0"
    assert result.input_tokens == 120
    assert result.output_tokens == 30
    assert "some long document text" in fake.calls[0]["messages"][0]["content"][0]["text"]


def test_reasoning_scratchpad_is_stripped() -> None:
    fake = FakeBedrock([converse_response("<think>step by step...</think>The answer.")])
    client = SummarizerClient(bedrock_client=fake)
    assert client.summarize("doc").summary == "The answer."


def test_retries_on_throttle_then_succeeds() -> None:
    sleeps: list[float] = []
    fake = FakeBedrock([throttle(), throttle(), converse_response("ok")])
    client = SummarizerClient(bedrock_client=fake, sleep=sleeps.append)

    result = client.summarize("doc")

    assert result.summary == "ok"
    assert len(fake.calls) == 3
    assert sleeps == [1, 2]


def test_gives_up_after_max_retries() -> None:
    fake = FakeBedrock([throttle()] * 4)
    client = SummarizerClient(bedrock_client=fake, max_retries=3, sleep=lambda _: None)

    with pytest.raises(SummarizationError, match="ThrottlingException"):
        client.summarize("doc")
    assert len(fake.calls) == 4


def test_non_retryable_error_fails_immediately() -> None:
    error = ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "Converse")
    fake = FakeBedrock([error])
    client = SummarizerClient(bedrock_client=fake, sleep=lambda _: None)

    with pytest.raises(SummarizationError, match="ValidationException"):
        client.summarize("doc")
    assert len(fake.calls) == 1
