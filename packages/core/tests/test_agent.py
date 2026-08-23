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


# ── The guardrail on the agentic path ───────────────────────────────────────
# A provisioned guardrail enforces nothing on its own; it applies only to calls
# that name it. `llm.py` has passed `guardrailConfig` since the guardrail was
# built, and this loop did not — so the best-performing path, the one a chat
# would ship, ran with no safety filter from the day it was written until
# 2026-08-23. Its non-diagnostic stance rested on a system prompt alone.


def guardrail_blocked(text: str = "I can't help with that.") -> dict[str, Any]:
    """A Converse response with the guardrail's own replacement text."""
    return {
        "stopReason": "guardrail_intervened",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": 40, "outputTokens": 12},
    }


def test_no_guardrail_configured_sends_no_guardrail_config() -> None:
    """Unset must mean absent, not an empty block Bedrock would reject."""
    bedrock = FakeBedrock([end_turn("Drink water.")])
    agent(bedrock, FakeKbRuntime([])).ask("hydration?")

    assert "guardrailConfig" not in bedrock.calls[0]


def test_the_guardrail_reaches_every_converse_call_not_just_the_last() -> None:
    """The model can emit text on a tool-use turn, so an intervention has to be
    able to stop the loop before another retrieval round — not only when the
    final answer is composed."""
    bedrock = FakeBedrock([tool_turn(("t1", "scalene anatomy", None)), end_turn("The scalenes …")])
    session = agent(
        bedrock,
        FakeKbRuntime([kb_result("Scalenes attach to the first rib.", "graph/scalenes.md")]),
        guardrail_id="gr-1",
        guardrail_version="3",
    ).ask("scalenes?")

    assert len(bedrock.calls) == 2  # one tool round, then the answer
    for call in bedrock.calls:
        assert call["guardrailConfig"] == {
            "guardrailIdentifier": "gr-1",
            "guardrailVersion": "3",
        }
    assert not session.guardrail_intervened


def test_the_version_defaults_to_draft_like_the_single_shot_path() -> None:
    bedrock = FakeBedrock([end_turn("ok")])
    agent(bedrock, FakeKbRuntime([]), guardrail_id="gr-1").ask("hi")

    assert bedrock.calls[0]["guardrailConfig"]["guardrailVersion"] == "DRAFT"


def test_an_intervention_stops_the_loop_and_is_reported() -> None:
    """The refusal must be distinguishable from a reply. Both are non-empty
    strings and the guardrail's text reads like an ordinary answer, so a caller
    that logs or scores answers cannot tell them apart without this flag."""
    bedrock = FakeBedrock([guardrail_blocked("I can't provide a diagnosis.")])
    session = agent(bedrock, FakeKbRuntime([]), guardrail_id="gr-1").ask("do I have a torn ACL?")

    assert session.guardrail_intervened
    assert session.answer == "I can't provide a diagnosis."
    assert session.tool_calls == []
    assert len(bedrock.calls) == 1  # stopped, did not iterate on


def test_an_intervention_mid_loop_does_not_retrieve_again() -> None:
    """Blocked on the second turn, after one tool round. The loop must not run
    another retrieval — an intervention is a stop, not a skipped turn."""
    kb = FakeKbRuntime([kb_result("Scalenes attach to the first rib.", "graph/scalenes.md")])
    bedrock = FakeBedrock([tool_turn(("t1", "scalenes", None)), guardrail_blocked()])
    session = agent(bedrock, kb, guardrail_id="gr-1").ask("scalenes?")

    assert session.guardrail_intervened
    assert len(session.tool_calls) == 1  # the one before the block, and no more
    assert len(bedrock.calls) == 2


def test_a_normal_answer_is_not_reported_as_intervened() -> None:
    bedrock = FakeBedrock([end_turn("The scalenes attach to the first rib.")])
    session = agent(bedrock, FakeKbRuntime([]), guardrail_id="gr-1").ask("scalenes?")

    assert not session.guardrail_intervened


def test_with_a_guardrail_only_the_question_is_guard_content_tagged() -> None:
    """Untagged, Converse input-assesses EVERY user turn — including toolResult
    turns carrying retrieved passages, which is where the blocking actually
    came from (guardrail_probe, 2026-08-24: corpus chunks block as input via
    MedicalDiagnosis and PROMPT_ATTACK; bare questions block 3/66). The tag
    narrows input assessment to the question; output stays assessed."""
    kb = FakeKbRuntime([kb_result("scalene anatomy", "anatomy/scalenes.md")])
    bedrock = FakeBedrock([tool_turn(("t1", "scalenes", None)), end_turn("done")])
    agent(bedrock, kb, guardrail_id="gr-1").ask("neck?")

    first = bedrock.calls[0]["messages"][0]
    assert first["content"] == [{"guardContent": {"text": {"text": "neck?"}}}]
    tool_result_turn = bedrock.calls[1]["messages"][-1]
    assert "toolResult" in tool_result_turn["content"][0]
    assert "guardContent" not in str(tool_result_turn)


def test_without_a_guardrail_the_question_stays_plain_text() -> None:
    """guardContent without a guardrailConfig is a Converse validation error."""
    bedrock = FakeBedrock([end_turn("hi")])
    agent(bedrock, FakeKbRuntime([])).ask("neck?")

    assert bedrock.calls[0]["messages"][0]["content"] == [{"text": "neck?"}]


# ── The verbatim-first seed ─────────────────────────────────────────────────
# FINDINGS six-run entry (2026-08-20): single-shot ∪ agentic = 61/61 answerable
# while neither alone reached it — the model's reformulations sometimes lose
# what embedding the verbatim question finds. The seed makes call 1 the user's
# question as a protocol fact rather than a prompt the model may ignore.


def test_verbatim_first_seeds_call_one_with_the_exact_question() -> None:
    bedrock = FakeBedrock([end_turn("Grounded answer.")])
    kb_runtime = FakeKbRuntime([kb_result("seed passage", "anatomy/seed.md")])
    session = agent(bedrock, kb_runtime, verbatim_first=True).ask("why does my neck click?")

    # The retrieval ran with the question verbatim and is a scored tool call.
    assert session.tool_calls[0].query == "why does my neck click?"
    assert session.tool_calls[0].passages[0].text == "seed passage"

    # The model's first Converse call sees the seed as a normal history pair:
    # assistant toolUse, then a user toolResult with the SAME id.
    history = bedrock.calls[0]["messages"]
    tool_use = history[1]["content"][0]["toolUse"]
    assert history[1]["role"] == "assistant"
    assert tool_use["name"] == "search_kb"
    assert tool_use["input"] == {"query": "why does my neck click?"}
    tool_result = history[2]["content"][0]["toolResult"]
    assert history[2]["role"] == "user"
    assert tool_result["toolUseId"] == tool_use["toolUseId"]
    assert "seed passage" in tool_result["content"][0]["text"]

    # And the system prompt says it already happened, so the model reformulates
    # instead of repeating the identical query.
    assert "already been searched" in bedrock.calls[0]["system"][0]["text"]


def test_seed_then_model_reformulations_accumulate_in_order() -> None:
    bedrock = FakeBedrock([tool_turn(("id-1", "cervical crepitus", None)), end_turn("Both.")])
    kb_runtime = FakeKbRuntime([kb_result("p", "anatomy/x.md")])
    session = agent(bedrock, kb_runtime, verbatim_first=True).ask("neck click?")

    assert [t.query for t in session.tool_calls] == ["neck click?", "cervical crepitus"]
    assert len(kb_runtime.calls) == 2


def test_without_the_flag_no_seed_happens() -> None:
    bedrock = FakeBedrock([end_turn("ok")])
    kb_runtime = FakeKbRuntime([])
    session = agent(bedrock, kb_runtime).ask("q")

    assert session.tool_calls == []
    assert kb_runtime.calls == []
    assert len(bedrock.calls[0]["messages"]) == 1
    assert "already been searched" not in bedrock.calls[0]["system"][0]["text"]
