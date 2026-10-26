"""Shared test doubles for Bedrock Converse and Knowledge Base retrieval.

One home for the fakes: ``test_agent``, ``test_chat_service`` and ``test_chat``
all script Converse turn by turn, and a fake copied into a second file is a
fake that drifts.
"""

from typing import Any

from botocore.exceptions import ClientError

BUCKET = "s3://kb-source/"


def converse_response(text: str) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
        "stopReason": "end_turn",
    }


def throttle() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse"
    )


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


def kb_result(
    text: str, key: str, score: float = 0.9, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One Retrieve result; ``metadata`` adds sidecar attributes (docTitle, …)."""
    return {
        "content": {"text": text},
        "score": score,
        "location": {"s3Location": {"uri": BUCKET + "corpus/" + key}},
        "metadata": {"x-amz-bedrock-kb-source-uri": BUCKET + "corpus/" + key, **(metadata or {})},
    }


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


def guardrail_blocked(text: str = "I can't help with that.") -> dict[str, Any]:
    """A Converse response with the guardrail's own replacement text."""
    return {
        "stopReason": "guardrail_intervened",
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": 40, "outputTokens": 12},
    }


def client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "op")
