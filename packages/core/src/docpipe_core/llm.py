"""Bedrock summarization client.

Uses the model-agnostic Converse API, so the model is pure configuration —
DeepSeek-R1 (`us.deepseek.r1-v1:0`) by default, swappable to any Bedrock
model ID without code changes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore.exceptions import ClientError

from docpipe_core.models import SummaryResult

DEFAULT_MODEL_ID = "us.deepseek.r1-v1:0"

_RETRYABLE_ERRORS = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
}

# Reasoning models may inline their scratchpad; the answer follows it.
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)

_PROMPT_TEMPLATE = (
    "Summarize the following document in at most {max_words} words. "
    "Reply with the summary only — no preamble.\n\n{text}"
)


class SummarizationError(Exception):
    """Raised when Bedrock invocation fails after retries."""


class SummarizerClient:
    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        bedrock_client: Any = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = model_id
        self._bedrock = bedrock_client or boto3.client("bedrock-runtime")
        self._max_retries = max_retries
        self._sleep = sleep

    def summarize(self, text: str, max_words: int = 150) -> SummaryResult:
        prompt = _PROMPT_TEMPLATE.format(max_words=max_words, text=text)
        started = time.monotonic()
        response = self._converse_with_retry(prompt)
        latency_ms = int((time.monotonic() - started) * 1000)

        summary = self._extract_text(response)
        usage = response.get("usage", {})
        return SummaryResult(
            summary=summary,
            model_id=self.model_id,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
            latency_ms=latency_ms,
        )

    def _converse_with_retry(self, prompt: str) -> dict[str, Any]:
        last_error: ClientError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._bedrock.converse(
                    modelId=self.model_id,
                    messages=[{"role": "user", "content": [{"text": prompt}]}],
                    inferenceConfig={"maxTokens": 2048, "temperature": 0.3},
                )
                return dict(response)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in _RETRYABLE_ERRORS or attempt == self._max_retries:
                    raise SummarizationError(f"Bedrock converse failed: {code}") from exc
                last_error = exc
                self._sleep(2**attempt)
        raise SummarizationError("Bedrock converse failed") from last_error

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(block["text"] for block in blocks if "text" in block)
        return _THINK_BLOCK.sub("", text).strip()
