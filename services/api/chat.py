"""``POST /chat`` — the Lambda behind the REST API Gateway (``infra/components/api.py``).

Thin by rule (README, repo layout): parse the proxy event, run one
``ChatService.turn``, shape the proxy response. Everything that could be wrong
about a chat turn lives in ``docpipe_core.chat`` with its tests; what can be
wrong here is the HTTP envelope, and ``test_api_chat.py`` pins that.

Status codes, and who produces them:

* ``200`` — the turn (``ChatResponse``, camelCase ``conversationId``).
* ``400`` — the body is not JSON or ``ChatRequest`` rejects it (missing
  ``userId``, empty ``message``…). The detail names the field, never echoes
  the input.
* ``429`` — never produced here. The usage plan's daily quota and the method
  throttle answer at the gateway, before this function runs; the client sees
  API Gateway's own ``{"message": "Limit Exceeded"}`` / ``"Too Many Requests"``.
* ``502`` — ``ModelInvocationError``: Bedrock (Retrieve or Converse) failed
  after the agent's retries, or the loop ended without an answer. The
  conversation is unchanged — ``ChatService`` raises before it persists.
* ``500`` — anything else, Lambda's own error path; the traceback is in the log.

Logging is structured JSON (``docpipe_core.observability``) and carries the
``conversationId``, latency, tokens, search count and the guardrail flag —
**never the message text and never the ``userId``**. The id is opaque but it is
still user data, and CloudWatch is not where it belongs.

Handler string for the Lambda: ``chat.handler``. The zip ``make lambda-zip``
builds places this file at the archive root next to ``docpipe_core/``.
"""

from __future__ import annotations

import base64
import json
from functools import cache
from typing import Any

from docpipe_core import (
    ChatRequest,
    ChatService,
    ModelInvocationError,
    configure_logging,
    get_logger,
)
from pydantic import ValidationError

configure_logging()
log = get_logger("docpipe.api.chat")

_JSON = {"content-type": "application/json"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint. The service is built once per container, from env."""
    return respond(event, service=_service())


@cache
def _service() -> ChatService:
    return ChatService.from_env()


def respond(event: dict[str, Any], *, service: Any) -> dict[str, Any]:
    """The whole request/response mapping, with the service injected for tests."""
    try:
        request = ChatRequest.model_validate_json(_body_of(event))
    except ValidationError as error:
        return _reply(400, {"error": "malformed body", "detail": _detail(error)})

    try:
        response = service.turn(request)
    except ModelInvocationError as error:
        log.error(
            "chat turn failed",
            extra={"context": {"conversationId": request.conversation_id, "error": str(error)}},
        )
        return _reply(
            502,
            {"error": "model invocation failed", "conversationId": request.conversation_id},
        )

    log.info(
        "chat turn",
        extra={
            "context": {
                "conversationId": response.conversation_id,
                "latency_ms": response.latency_ms,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "tool_calls": response.tool_calls,
                "citations": len(response.citations),
                "guardrail_intervened": response.guardrail_intervened,
            }
        },
    )
    return _reply(200, response.model_dump(by_alias=True))


def _body_of(event: dict[str, Any]) -> str:
    body = event.get("body")
    if body is None:
        return ""  # pydantic reports it as invalid JSON → 400
    if event.get("isBase64Encoded"):
        return base64.b64decode(body).decode("utf-8")
    return str(body)


def _detail(error: ValidationError) -> list[dict[str, Any]]:
    # Field and message only. `include_input=False` keeps the user's text out
    # of the response and, through API Gateway's access log, out of CloudWatch.
    return [
        {"loc": [str(part) for part in item["loc"]], "msg": item["msg"]}
        for item in error.errors(include_url=False, include_input=False)
    ]


def _reply(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": status, "headers": _JSON, "body": json.dumps(payload)}
