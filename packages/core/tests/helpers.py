"""Shared test doubles for Bedrock."""

from typing import Any

from botocore.exceptions import ClientError


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
