import pytest
from botocore.exceptions import ClientError
from docpipe_core.llm import ModelInvocationError, SummarizerClient
from helpers import FakeBedrock, converse_response, throttle


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
    client = SummarizerClient(bedrock_client=fake, sleep=sleeps.append, rng=lambda: 0.25)

    result = client.summarize("doc")

    assert result.summary == "ok"
    assert len(fake.calls) == 3
    assert sleeps == [1.25, 2.25]  # 2**attempt floor + injected jitter


def test_backoff_jitter_desynchronises_a_throttled_fleet() -> None:
    """Every wait is its power-of-two floor plus up to 1 s of jitter.

    Without the jitter, N Lambdas throttled in the same second sleep the same
    1 s, 2 s, 4 s — and slam Bedrock together on every retry, refailing as a
    synchronised wave (M8 cross-exam item 4).
    """
    sleeps: list[float] = []
    fake = FakeBedrock([throttle(), throttle(), converse_response("ok")])
    SummarizerClient(bedrock_client=fake, sleep=sleeps.append).summarize("doc")

    assert len(sleeps) == 2
    assert all(2**i <= s < 2**i + 1 for i, s in enumerate(sleeps))


def test_gives_up_after_max_retries() -> None:
    fake = FakeBedrock([throttle()] * 4)
    client = SummarizerClient(bedrock_client=fake, max_retries=3, sleep=lambda _: None)

    with pytest.raises(ModelInvocationError, match="ThrottlingException"):
        client.summarize("doc")
    assert len(fake.calls) == 4


def test_non_retryable_error_fails_immediately() -> None:
    error = ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "Converse")
    fake = FakeBedrock([error])
    client = SummarizerClient(bedrock_client=fake, sleep=lambda _: None)

    with pytest.raises(ModelInvocationError, match="ValidationException"):
        client.summarize("doc")
    assert len(fake.calls) == 1
