"""ChatService: the turn the Lambda wraps — persistence, citations, failure shape.

The store is moto's DynamoDB (real key semantics), the model and the KB are the
shared fakes. User ids are synthetic by rule: this repo is public.
"""

from __future__ import annotations

import string
from itertools import cycle
from typing import Any

import pytest
from docpipe_core.agent import KbAgent
from docpipe_core.chat import (
    CHAT_SYSTEM,
    NOT_COVERED_MARKER,
    SYNTHETIC_USER_ID,
    ChatRequest,
    ChatResponse,
    ChatService,
    Citation,
)
from docpipe_core.conversations import ConversationStore
from docpipe_core.llm import ModelInvocationError
from docpipe_core.retrieval import KnowledgeBaseClient
from helpers import (
    FakeBedrock,
    FakeKbRuntime,
    client_error,
    end_turn,
    guardrail_blocked,
    kb_result,
    tool_turn,
)
from pydantic import ValidationError

USER = "smoke-0badc0de"
TABLE = "docpipe-conversations"

# A sidecar as `pnpm kb:build` writes it — the seven stamp keys.
STAMP: dict[str, Any] = {
    "docTitle": "Pain Science",
    "section": "Central sensitization",
    "sourcePath": "pain-science/03-central-sensitization.md",
    "maxEvidence": 2,
    "verification": ["systematic-review", "guideline"],
    "citationCount": 3,
    "safetyCritical": False,
}
STAMPED = kb_result("Pain can persist after tissue heals.", "pain/03.md", metadata=STAMP)
UNSTAMPED = kb_result("Legacy chunk without a sidecar.", "legacy/old.md")


def service(
    table: Any, bedrock: FakeBedrock, kb_runtime: FakeKbRuntime, **kwargs: Any
) -> ChatService:
    history_limit = kwargs.pop("history_limit", 10)
    kb = KnowledgeBaseClient("kb-1", agent_runtime_client=kb_runtime)
    agent = KbAgent(
        kb,
        bedrock_client=bedrock,
        sleep=lambda _: None,
        rng=lambda: 0.5,
        clock=cycle([0.0, 1.5]).__next__,
        **kwargs,
    )
    store = ConversationStore(TABLE, dynamodb_resource=table)
    return ChatService(agent, store, history_limit=history_limit, clock=cycle([0.0, 0.25]).__next__)


def request(message: str, conversation_id: str | None = None) -> ChatRequest:
    return ChatRequest(userId=USER, message=message, conversationId=conversation_id)


def stored(table: Any, conversation_id: str) -> list[tuple[str, str]]:
    conversation = ConversationStore(TABLE, dynamodb_resource=table).get(USER, conversation_id)
    return [] if conversation is None else [(m.role, m.content) for m in conversation.messages]


def test_a_first_turn_mints_a_conversation_and_persists_both_messages(conversations_table) -> None:
    bedrock = FakeBedrock([end_turn("Pain can outlast healing.", tokens=(300, 40))])
    svc = service(conversations_table, bedrock, FakeKbRuntime([STAMPED]), verbatim_first=True)

    response = svc.turn(request("why does it still hurt?"))

    assert len(response.conversation_id) == 32
    assert set(response.conversation_id) <= set(string.hexdigits.lower())
    assert response.reply == "Pain can outlast healing."
    assert stored(conversations_table, response.conversation_id) == [
        ("user", "why does it still hurt?"),
        ("assistant", "Pain can outlast healing."),
    ]
    assert response.model_id == svc.agent.model_id
    assert (response.input_tokens, response.output_tokens) == (300, 40)
    assert response.latency_ms == 250
    assert response.tool_calls == 1  # the verbatim seed
    assert not response.guardrail_intervened


def test_a_follow_up_replays_the_stored_history_before_the_question(conversations_table) -> None:
    bedrock = FakeBedrock([end_turn("The scalenes."), end_turn("Yes, both sides.")])
    svc = service(conversations_table, bedrock, FakeKbRuntime([]))

    first = svc.turn(request("what connects neck and shoulder?"))
    second = svc.turn(request("on both sides?", first.conversation_id))

    assert second.conversation_id == first.conversation_id
    replay = bedrock.calls[1]["messages"]
    assert [m["role"] for m in replay] == ["user", "assistant", "user"]
    assert replay[0]["content"] == [{"text": "what connects neck and shoulder?"}]
    assert replay[1]["content"] == [{"text": "The scalenes."}]
    assert replay[2]["content"] == [{"text": "on both sides?"}]
    assert stored(conversations_table, first.conversation_id) == [
        ("user", "what connects neck and shoulder?"),
        ("assistant", "The scalenes."),
        ("user", "on both sides?"),
        ("assistant", "Yes, both sides."),
    ]


def test_history_is_capped_in_whole_turns(conversations_table) -> None:
    """An odd cap must not replay a dangling assistant turn first — Converse
    requires the replay to open on the user."""
    bedrock = FakeBedrock([end_turn("a1"), end_turn("a2"), end_turn("a3")])
    svc = service(conversations_table, bedrock, FakeKbRuntime([]), history_limit=3)

    conv = svc.turn(request("q1")).conversation_id
    svc.turn(request("q2", conv))
    svc.turn(request("q3", conv))

    replay = bedrock.calls[2]["messages"]
    assert [(m["role"], m["content"][0]["text"]) for m in replay] == [
        ("user", "q2"),
        ("assistant", "a2"),
        ("user", "q3"),
    ]


def test_an_unknown_conversation_id_restarts_under_that_id(conversations_table) -> None:
    """TTL can delete a thread mid-conversation; the client keeps its id and
    gets a fresh history rather than an unrecoverable 404."""
    bedrock = FakeBedrock([end_turn("fresh")])
    svc = service(conversations_table, bedrock, FakeKbRuntime([]))

    response = svc.turn(request("hello again", "conv-expired"))

    assert response.conversation_id == "conv-expired"
    assert len(bedrock.calls[0]["messages"]) == 1  # no history replayed
    assert stored(conversations_table, "conv-expired") == [
        ("user", "hello again"),
        ("assistant", "fresh"),
    ]


def test_citations_carry_the_five_stamp_fields_and_dedupe_by_source_path(
    conversations_table,
) -> None:
    # Seed retrieval returns the stamped passage; the model's own search
    # returns it again plus an unstamped one. Three passages, two citations.
    bedrock = FakeBedrock([tool_turn(("t1", "central sensitization", 1)), end_turn("Grounded.")])
    kb_runtime = FakeKbRuntime([STAMPED, UNSTAMPED])
    svc = service(conversations_table, bedrock, kb_runtime, verbatim_first=True)

    response = svc.turn(request("why does pain persist?"))

    assert response.tool_calls == 2
    assert len(response.citations) == 2
    stamped, unstamped = response.citations
    assert stamped == Citation(
        source_path="pain-science/03-central-sensitization.md",
        doc_title="Pain Science",
        section="Central sensitization",
        max_evidence=2,
        verification=["systematic-review", "guideline"],
        label="Pain Science → Central sensitization (★★☆)",
    )
    # A chunk without a sidecar still cites — by its source URI — with the
    # stamp fields honestly None rather than invented.
    assert unstamped.source_path is None
    assert unstamped.max_evidence is None
    assert unstamped.label.endswith("corpus/legacy/old.md")

    wire = response.model_dump(by_alias=True)
    assert {"source_path", "doc_title", "section", "max_evidence", "verification"} <= set(
        wire["citations"][0]
    )


def test_a_model_error_is_reported_and_leaves_the_conversation_unchanged(
    conversations_table,
) -> None:
    bedrock = FakeBedrock([end_turn("first"), client_error("ValidationException", "bad shape")])
    svc = service(conversations_table, bedrock, FakeKbRuntime([]))
    conv = svc.turn(request("q1")).conversation_id
    before = stored(conversations_table, conv)

    with pytest.raises(ModelInvocationError, match="ValidationException"):
        svc.turn(request("q2", conv))

    assert stored(conversations_table, conv) == before == [("user", "q1"), ("assistant", "first")]


def test_a_retrieve_failure_is_reported_the_same_way(conversations_table) -> None:
    """The seed retrieval runs before any Converse call; a 403 there is a
    backend failure like any other — 502, nothing persisted."""
    denied = client_error("AccessDeniedException", "not authorized: bedrock:Retrieve")
    svc = service(
        conversations_table,
        FakeBedrock([]),
        FakeKbRuntime([], errors=[denied]),
        verbatim_first=True,
    )

    with pytest.raises(ModelInvocationError, match="AccessDeniedException"):
        svc.turn(request("q", "conv-1"))

    assert stored(conversations_table, "conv-1") == []


def test_an_iteration_bound_without_an_answer_is_a_model_error(conversations_table) -> None:
    bedrock = FakeBedrock([tool_turn(("t", "again", None)) for _ in range(3)])
    svc = service(conversations_table, bedrock, FakeKbRuntime([]), max_iterations=2)

    with pytest.raises(ModelInvocationError, match="without an answer"):
        svc.turn(request("q", "conv-1"))

    assert stored(conversations_table, "conv-1") == []


def test_a_guardrail_intervention_is_flagged_and_the_refusal_is_persisted(
    conversations_table,
) -> None:
    bedrock = FakeBedrock([guardrail_blocked("I can't diagnose that.")])
    svc = service(conversations_table, bedrock, FakeKbRuntime([]), guardrail_id="gr-1")

    response = svc.turn(request("do I have a torn ACL?"))

    assert response.guardrail_intervened
    assert response.reply == "I can't diagnose that."
    assert stored(conversations_table, response.conversation_id)[1] == (
        "assistant",
        "I can't diagnose that.",
    )


def test_the_response_uses_the_wire_names() -> None:
    wire = ChatResponse(
        reply="r",
        citations=[],
        conversation_id="conv-1",
        model_id="m",
        guardrail_intervened=False,
        latency_ms=1,
        input_tokens=2,
        output_tokens=3,
        tool_calls=1,
    ).model_dump(by_alias=True)
    assert wire["conversationId"] == "conv-1"
    assert "conversation_id" not in wire
    assert {"reply", "citations", "model_id", "guardrail_intervened", "latency_ms"} <= set(wire)


def test_the_request_parses_the_wire_shape() -> None:
    parsed = ChatRequest.model_validate_json(
        f'{{"userId": "{USER}", "message": "hi", "conversationId": "conv-1"}}'
    )
    assert (parsed.user_id, parsed.message, parsed.conversation_id) == (USER, "hi", "conv-1")
    assert (
        ChatRequest.model_validate_json(f'{{"userId": "{USER}", "message": "hi"}}').conversation_id
        is None
    )


@pytest.mark.parametrize(
    "body",
    [
        '{"message": "no user"}',
        f'{{"userId": "{USER}"}}',
        f'{{"userId": "{USER}", "message": ""}}',
        '{"userId": "", "message": "x"}',
        f'{{"userId": "{USER}", "message": "x", "conversationId": ""}}',
        "not json",
    ],
)
def test_a_malformed_request_is_a_validation_error(body: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest.model_validate_json(body)


@pytest.mark.parametrize("user_id", ["smoke-0badc0de", "smoke-00000000", "smoke-ffffffff"])
def test_synthetic_user_ids_match(user_id: str) -> None:
    assert SYNTHETIC_USER_ID.match(user_id)


@pytest.mark.parametrize(
    "user_id", ["smoke-0BADC0DE", "smoke-0badc0d", "smoke-0badc0de1", "user-0badc0de", "", "smoke-"]
)
def test_anything_else_is_not_synthetic(user_id: str) -> None:
    assert not SYNTHETIC_USER_ID.match(user_id)


def test_the_system_prompt_states_the_not_covered_phrasing() -> None:
    assert NOT_COVERED_MARKER in CHAT_SYSTEM
    assert "do not diagnose" in CHAT_SYSTEM  # the health prompt is the base, not replaced


def test_from_env_wires_the_lambda_configuration() -> None:
    env = {
        "KNOWLEDGE_BASE_ID": "kb-env",
        "CONVERSATIONS_TABLE": TABLE,
        "CHAT_MODEL_ID": "deepseek.v3.2",
        "GUARDRAIL_ID": "gr-env",
        "GUARDRAIL_VERSION": "2",
        "CHAT_MAX_ITERATIONS": "4",
        "CHAT_TOP_K": "3",
    }
    svc = ChatService.from_env(env)

    assert svc.agent.kb.knowledge_base_id == "kb-env"
    assert svc.agent.model_id == "deepseek.v3.2"
    assert svc.agent.verbatim_first is True
    assert svc.agent.max_iterations == 4
    assert svc.agent.top_k == 3
    # The guardrail and the chat prompt reach the Converse call; the only
    # observable is the request shape, so read the agent's prepared state.
    assert svc.agent._guardrail_kwargs()["guardrailConfig"] == {
        "guardrailIdentifier": "gr-env",
        "guardrailVersion": "2",
    }
    assert svc.agent._system.startswith(CHAT_SYSTEM)


def test_from_env_without_a_guardrail_sends_none() -> None:
    svc = ChatService.from_env({"KNOWLEDGE_BASE_ID": "kb", "CONVERSATIONS_TABLE": TABLE})
    assert svc.agent._guardrail_kwargs() == {}


@pytest.mark.parametrize("missing", ["KNOWLEDGE_BASE_ID", "CONVERSATIONS_TABLE"])
def test_from_env_names_the_missing_variable(missing: str) -> None:
    env = {"KNOWLEDGE_BASE_ID": "kb", "CONVERSATIONS_TABLE": TABLE}
    del env[missing]
    with pytest.raises(RuntimeError, match=missing):
        ChatService.from_env(env)
