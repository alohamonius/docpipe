"""The Lambda envelope around ChatService — services/api/chat.py.

What can be wrong here is the HTTP mapping, so that is what is pinned: 400 for a
body the request model rejects, 502 for a model failure, 200 with the wire names,
base64 bodies decoded, and the entrypoint wired to the cached service.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import chat  # services/api/chat.py — on sys.path via pyproject's pytest `pythonpath`
import pytest
from docpipe_core.chat import ChatRequest, ChatResponse
from docpipe_core.llm import ModelInvocationError

USER = "smoke-0badc0de"

OK = ChatResponse(
    reply="The scalenes.",
    citations=[],
    conversation_id="conv-1",
    model_id="deepseek.v3.2",
    guardrail_intervened=False,
    latency_ms=1200,
    input_tokens=900,
    output_tokens=40,
    tool_calls=1,
)


class FakeService:
    def __init__(self, outcome: ChatResponse | Exception) -> None:
        self.outcome = outcome
        self.requests: list[ChatRequest] = []

    def turn(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def event(body: str | None, *, b64: bool = False) -> dict[str, Any]:
    if body is not None and b64:
        body = base64.b64encode(body.encode()).decode()
    return {"httpMethod": "POST", "path": "/chat", "body": body, "isBase64Encoded": b64}


def body_of(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["headers"] == {"content-type": "application/json"}
    return dict(json.loads(reply["body"]))


def test_a_happy_turn_is_a_200_with_the_wire_names() -> None:
    service = FakeService(OK)
    reply = chat.respond(
        event(json.dumps({"userId": USER, "message": "neck?", "conversationId": "conv-1"})),
        service=service,
    )

    assert reply["statusCode"] == 200
    payload = body_of(reply)
    assert payload["conversationId"] == "conv-1"
    assert payload["reply"] == "The scalenes."
    assert {"model_id", "guardrail_intervened", "latency_ms", "citations"} <= set(payload)
    assert "conversation_id" not in payload
    [request] = service.requests
    assert (request.user_id, request.message, request.conversation_id) == (USER, "neck?", "conv-1")


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "not json",
        '{"message": "no user"}',
        f'{{"userId": "{USER}", "message": ""}}',
        "[1, 2]",
    ],
)
def test_a_malformed_body_is_a_400_and_never_reaches_the_service(body: str | None) -> None:
    service = FakeService(OK)
    reply = chat.respond(event(body), service=service)

    assert reply["statusCode"] == 400
    payload = body_of(reply)
    assert payload["error"] == "malformed body"
    assert payload["detail"] and all({"loc", "msg"} <= set(item) for item in payload["detail"])
    assert service.requests == []


def test_a_400_names_the_field_but_not_the_input() -> None:
    reply = chat.respond(event('{"message": "my private words"}'), service=FakeService(OK))
    payload = body_of(reply)
    assert ["userId"] in [item["loc"] for item in payload["detail"]]
    assert "my private words" not in reply["body"]


def test_a_base64_body_is_decoded_first() -> None:
    service = FakeService(OK)
    reply = chat.respond(
        event(json.dumps({"userId": USER, "message": "hi"}), b64=True), service=service
    )
    assert reply["statusCode"] == 200
    assert service.requests[0].message == "hi"


def test_a_model_error_is_a_502_naming_the_conversation_and_no_text() -> None:
    service = FakeService(ModelInvocationError("Bedrock call failed: ThrottlingException"))
    reply = chat.respond(
        event(json.dumps({"userId": USER, "message": "secret text", "conversationId": "conv-9"})),
        service=service,
    )

    assert reply["statusCode"] == 502
    payload = body_of(reply)
    assert payload == {"error": "model invocation failed", "conversationId": "conv-9"}
    assert "secret text" not in reply["body"]


def test_the_entrypoint_uses_the_cached_service(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService(OK)
    monkeypatch.setattr(chat, "_service", lambda: service)
    reply = chat.handler(event(json.dumps({"userId": USER, "message": "hi"})), None)
    assert reply["statusCode"] == 200
    assert len(service.requests) == 1
