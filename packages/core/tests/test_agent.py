"""The tool loop's protocol, pinned.

Every case here is a behavior that failed live first: the parallel-toolUse
crash, the Aurora-resume ValidationException, the iteration bound. The fakes
script Converse turn by turn, so the loop's message bookkeeping — the part
Bedrock validates server-side — is asserted byte-for-byte here instead.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError
from docpipe_core.agent import AgentSession, KbAgent
from docpipe_core.retrieval import KnowledgeBaseClient

BUCKET = "s3://kb-source/"


def kb_result(text: str, key: str, score: float = 0.9) -> dict[str, Any]:
    return {
        "content": {"text": text},
        "score": score,
        "location": {"s3Location": {"uri": BUCKET + "corpus/" + key}},
        "metadata": {"x-amz-bedrock-kb-source-uri": BUCKET + "corpus/" + key},
    }


class FakeKbRuntime:
    """bedrock-agent-runtime: canned results, optionally erroring first."""

    def __init__(self, results: list[dict[str, Any]], errors: list[Exception] | None = None):
        self.results = results
        self.errors = list(errors or [])
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return {"retrievalResults": self.results}


class FakeBedrock:
    """bedrock-runtime: a script of converse responses (or exceptions)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return dict(step)


def end_turn(text: str, tokens: tuple[int, int] = (100, 20)) -> dict[str, Any]:
    return {
        "stopReason": "end_turn",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
    }


def tool_turn(*calls: tuple[str, str, int | None]) -> dict[str, Any]:
    """(toolUseId, query, min_evidence) per parallel call."""
    content: list[dict[str, Any]] = [{"text": "let me search"}]
    for tool_use_id, query, floor in calls:
        tool_input: dict[str, Any] = {"query": query}
        if floor is not None:
            tool_input["min_evidence"] = floor
        content.append(
            {"toolUse": {"toolUseId": tool_use_id, "name": "search_kb", "input": tool_input}}
        )
    return {
        "stopReason": "tool_use",
        "output": {"message": {"role": "assistant", "content": content}},
        "usage": {"inputTokens": 200, "outputTokens": 40},
    }


def client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "op")


def agent(bedrock: FakeBedrock, kb_runtime: FakeKbRuntime, **kwargs: Any) -> KbAgent:
    kb = KnowledgeBaseClient("kb-123", agent_runtime_client=kb_runtime)
    naps: list[float] = kwargs.pop("naps", [])
    return KbAgent(
        kb,
        bedrock_client=bedrock,
        sleep=naps.append,
        rng=lambda: 0.5,
        clock=iter([0.0, 1.5]).__next__,
        **kwargs,
    )


def test_an_immediate_answer_needs_no_tools_and_strips_the_scratchpad() -> None:
    bedrock = FakeBedrock([end_turn("<think>hmm</think>Drink water.")])
    session = agent(bedrock, FakeKbRuntime([])).ask("hydration?")

    assert session.answer == "Drink water."
    assert session.tool_calls == []
    assert not session.hit_iteration_bound
    assert (session.input_tokens, session.output_tokens) == (100, 20)


def test_one_tool_round_trip_pairs_result_to_the_same_id() -> None:
    bedrock = FakeBedrock([tool_turn(("id-1", "neck anatomy", 1)), end_turn("The trapezius.")])
    kb_runtime = FakeKbRuntime([kb_result("passage text", "anatomy/traps.md")])
    session = agent(bedrock, kb_runtime, search_type=None).ask("what connects neck?")

    # The retrieval happened with the model's arguments, rerank off.
    retrieval = kb_runtime.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert retrieval["numberOfResults"] == 5
    assert "rerankingConfiguration" not in retrieval
    assert session.tool_calls[0].query == "neck anatomy"
    assert session.tool_calls[0].min_evidence == 1
    assert session.tool_calls[0].passages[0].text == "passage text"

    # History replayed to call 2: assistant toolUse verbatim, then ONE user
    # message whose toolResult carries the SAME toolUseId.
    replay = bedrock.calls[1]["messages"]
    assert replay[1]["role"] == "assistant"
    tool_result = replay[2]["content"][0]["toolResult"]
    assert replay[2]["role"] == "user"
    assert tool_result["toolUseId"] == "id-1"
    assert "passage text" in tool_result["content"][0]["text"]
    assert session.answer == "The trapezius."


def test_parallel_tool_uses_come_back_in_a_single_user_message() -> None:
    """V3.2 emits several toolUse blocks in one turn; Converse rejects anything
    but ALL their toolResults in one following user message. This crashed a
    live run before it was handled — the regression this test pins."""
    bedrock = FakeBedrock(
        [tool_turn(("id-a", "first", None), ("id-b", "second", 2)), end_turn("Both found.")]
    )
    kb_runtime = FakeKbRuntime([kb_result("p", "anatomy/x.md")])
    session = agent(bedrock, kb_runtime).ask("q")

    replay = bedrock.calls[1]["messages"]
    results = [c["toolResult"]["toolUseId"] for c in replay[2]["content"]]
    assert results == ["id-a", "id-b"]
    assert len(session.tool_calls) == 2
    assert [t.query for t in session.tool_calls] == ["first", "second"]


def test_the_iteration_bound_is_ours_not_the_models() -> None:
    bedrock = FakeBedrock([tool_turn(("id", "again", None)) for _ in range(9)])
    kb_runtime = FakeKbRuntime([])
    session = agent(bedrock, kb_runtime, max_iterations=3).ask("q")

    assert len(bedrock.calls) == 3
    assert session.hit_iteration_bound
    assert session.answer == ""


def test_a_throttled_converse_is_retried_with_jittered_backoff() -> None:
    naps: list[float] = []
    bedrock = FakeBedrock([client_error("ThrottlingException"), end_turn("ok")])
    session = agent(bedrock, FakeKbRuntime([]), naps=naps).ask("q")

    assert session.answer == "ok"
    assert naps == [1.5]  # 2**0 + rng() with rng pinned to 0.5


def test_the_aurora_resume_error_is_retried_on_the_retrieve_side() -> None:
    resume = client_error(
        "ValidationException",
        "The Aurora DB instance db-X is resuming after being auto-paused. Please wait",
    )
    bedrock = FakeBedrock([tool_turn(("id", "q", None)), end_turn("found")])
    kb_runtime = FakeKbRuntime([kb_result("p", "anatomy/x.md")], errors=[resume])
    session = agent(bedrock, kb_runtime, naps=[]).ask("q")

    assert session.answer == "found"
    assert len(kb_runtime.calls) == 2  # failed once, retried once


def test_a_genuinely_invalid_request_still_fails_fast() -> None:
    bedrock = FakeBedrock([client_error("ValidationException", "bad toolConfig shape")])
    with pytest.raises(ClientError):
        agent(bedrock, FakeKbRuntime([])).ask("q")
    assert len(bedrock.calls) == 1  # no retry


def test_a_session_serialises_for_reports() -> None:
    bedrock = FakeBedrock([end_turn("fine")])
    session = agent(bedrock, FakeKbRuntime([])).ask("q")
    assert isinstance(session, AgentSession)
    dumped = session.model_dump()
    assert dumped["wall_s"] == 1.5
    assert dumped["stop_reason"] == "end_turn"
