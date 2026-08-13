"""Bedrock clients: chat assistant + document summarization.

Both use the model-agnostic Converse API, so the model is pure
configuration — DeepSeek-R1 (`us.deepseek.r1-v1:0`) by default, swappable
to any Bedrock model ID without code changes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from typing import Any

import boto3
from botocore.exceptions import ClientError

from docpipe_core.models import ChatMessage, ChatReply, SummaryResult
from docpipe_core.retrieval import RetrievedPassage

DEFAULT_MODEL_ID = "us.deepseek.r1-v1:0"

_RETRYABLE_ERRORS = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
}

# Reasoning models may inline their scratchpad; the answer follows it.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)

_SUMMARY_PROMPT = (
    "Summarize the following document in at most {max_words} words. "
    "Reply with the summary only — no preamble.\n\n{text}"
)

# Non-diagnostic by design; mirrors the red-flag gate in the health.studio app.
HEALTH_ASSISTANT_SYSTEM = (
    "You are the health.studio assistant: supportive, evidence-aware guidance "
    "on training, recovery, mobility, and healthy habits. You do not diagnose "
    "conditions, prescribe treatment or medication, or interpret lab results. "
    "If the user describes sharp or worsening pain, numbness, chest pain, "
    "fever, or symptoms after trauma, tell them to see a clinician promptly "
    "instead of advising further. Keep answers concise and practical."
)


_GROUNDING_INSTRUCTIONS = (
    "\n\nGround your answer in the knowledge-base excerpts below when they are "
    "relevant, and name the source you relied on. If the excerpts do not cover "
    "the question, say so plainly instead of inventing facts.\n"
)


def _grounded_system(system: str, passages: Sequence[RetrievedPassage]) -> str:
    lines = [system, _GROUNDING_INSTRUCTIONS]
    for i, passage in enumerate(passages, start=1):
        source = f" (source: {passage.source})" if passage.source else ""
        lines.append(f"[{i}]{source} {passage.text}")
    return "\n".join(lines)


class ModelInvocationError(Exception):
    """Raised when Bedrock invocation fails after retries."""


class _ConverseClient:
    """Shared Converse call with retry/backoff and text extraction."""

    DEFAULT_MAX_TOKENS = 1024

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        bedrock_client: Any = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        max_tokens: int | None = None,
        guardrail_id: str | None = None,
        guardrail_version: str = "DRAFT",
    ) -> None:
        self.model_id = model_id
        self._bedrock = bedrock_client or boto3.client("bedrock-runtime")
        self._max_retries = max_retries
        self._sleep = sleep
        self._max_tokens = max_tokens if max_tokens is not None else self.DEFAULT_MAX_TOKENS
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version

    def _converse(
        self, messages: list[dict[str, Any]], system: str | None = None
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "inferenceConfig": {"maxTokens": self._max_tokens, "temperature": 0.3},
        }
        if system is not None:
            kwargs["system"] = [{"text": system}]
        # A provisioned guardrail enforces nothing on its own — it applies only
        # to calls that name it. Absent this block the Bedrock Guardrail in
        # `pulumi/components/safety.py` is decoration, and the non-diagnostic
        # stance rests entirely on a system prompt that prompt injection can
        # argue with. The role needs `bedrock:ApplyGuardrail` on the ARN.
        if self._guardrail_id:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": self._guardrail_id,
                "guardrailVersion": self._guardrail_version,
            }

        last_error: ClientError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return dict(self._bedrock.converse(**kwargs))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in _RETRYABLE_ERRORS or attempt == self._max_retries:
                    raise ModelInvocationError(f"Bedrock converse failed: {code}") from exc
                last_error = exc
                self._sleep(2**attempt)
        raise ModelInvocationError("Bedrock converse failed") from last_error

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block["text"] for block in blocks if "text" in block)
        return _THINK_BLOCK.sub("", text).strip()


class ChatClient(_ConverseClient):
    """Multi-turn health assistant chat (sync path: Lambda behind /chat)."""

    def reply(
        self,
        history: Sequence[ChatMessage],
        system: str = HEALTH_ASSISTANT_SYSTEM,
        passages: Sequence[RetrievedPassage] | None = None,
    ) -> ChatReply:
        if not history or history[-1].role != "user":
            raise ValueError("history must end with a user message")
        if passages:
            system = _grounded_system(system, passages)
        messages = [{"role": m.role, "content": [{"text": m.content}]} for m in history]
        started = time.monotonic()
        response = self._converse(messages, system=system)
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.get("usage", {})
        return ChatReply(
            message=self._extract_text(response),
            model_id=self.model_id,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            latency_ms=latency_ms,
            guardrail_intervened=response.get("stopReason") == "guardrail_intervened",
        )


class SummarizerClient(_ConverseClient):
    """Document summarization (async path: EKS worker)."""

    DEFAULT_MAX_TOKENS = 2048

    def summarize(self, text: str, max_words: int = 150) -> SummaryResult:
        prompt = _SUMMARY_PROMPT.format(max_words=max_words, text=text)
        started = time.monotonic()
        response = self._converse([{"role": "user", "content": [{"text": prompt}]}])
        latency_ms = int((time.monotonic() - started) * 1000)

        usage = response.get("usage", {})
        return SummaryResult(
            summary=self._extract_text(response),
            model_id=self.model_id,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            latency_ms=latency_ms,
        )
