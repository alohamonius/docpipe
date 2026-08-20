"""The Converse tool loop: the model drives retrieval, this module owns the protocol.

Promoted out of ``scripts/kb_agentic_eval.py`` (2026-08-21) because three
consumers now need the same loop — the agentic eval, the local playground, and
Phase 3's shipped agent — and the repo's own rule applies to itself: logic
nobody can unit-test is logic nobody should trust. The scripts stay thin.

What the loop is, measured (FINDINGS 2026-08-18 / the six-run 2026-08-20
matrix): ``deepseek.v3.2`` given a single ``search_kb(query, min_evidence)``
tool reformulates its own queries (~2.8–3.0 calls per question), sets its own
evidence floor from a one-line hint, and reaches 0.89–0.95 session recall
where single-shot retrieval scores 0.79 — at ~4 converse calls and ~14 s per
question. The loop is the effect; the store is not.

Protocol facts this module encodes so callers cannot re-learn them the hard
way:

* The assistant's ``toolUse`` blocks go back into history **verbatim** —
  dropping them is the classic validation error that looks like a model bug.
* V3.2 sometimes emits **several** ``toolUse`` blocks in one message
  (parallel searches, observed live 2026-08-20). Converse requires every one
  of their ``toolResult`` blocks in a **single** following user message; one
  message per result fails with "Expected toolResult blocks at
  messages.N.content".
* ``MAX_ITERATIONS`` is our bound, not the model's — ``end_turn`` is a hope,
  not a guarantee (first observed arriving at call 4 of 5).
* An Aurora KB resuming from min-ACU-0 auto-pause surfaces through
  ``Retrieve`` as a ``ValidationException`` whose message says "resuming
  after being auto-paused" — retryable, and *only* that one; a genuinely
  invalid request must still fail fast.

Rerank stays OFF inside the tool by default: ``cohere.rerank-v3-5`` is
3 req/min at ACCOUNT level, and an agent making three tool calls per question
would starve on it (FINDINGS 2026-08-17).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from docpipe_core.llm import HEALTH_ASSISTANT_SYSTEM, extract_answer_text
from docpipe_core.retrieval import KnowledgeBaseClient, RetrievedPassage

DEFAULT_AGENT_MODEL_ID = "deepseek.v3.2"  # ON_DEMAND + tool use; R1 has neither
DEFAULT_MAX_ITERATIONS = 5

SEARCH_TOOL_NAME = "search_kb"

# The one-line min_evidence hint is load-bearing: the 2026-08-18 run showed the
# model setting the floor on every call from nothing but this sentence.
_TOOL_CONFIG: dict[str, Any] = {
    "tools": [
        {
            "toolSpec": {
                "name": SEARCH_TOOL_NAME,
                "description": (
                    "Search the health.studio knowledge base for evidence-graded "
                    "passages about anatomy, pain, training and recovery. Pass "
                    "min_evidence=1 when the question is about the user's pain."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "min_evidence": {"type": "integer", "minimum": 0, "maximum": 3},
                        },
                        "required": ["query"],
                    }
                },
            }
        }
    ]
}

_RETRYABLE_CODES = frozenset(
    {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException"}
)


def _is_retryable(error: ClientError) -> bool:
    code = error.response.get("Error", {}).get("Code", "")
    if code in _RETRYABLE_CODES:
        return True
    return code == "ValidationException" and "resuming after being auto-paused" in str(error)


class ToolCall(BaseModel):
    """One ``search_kb`` invocation the model chose to make."""

    query: str
    min_evidence: int | None = None
    passages: list[RetrievedPassage]


class AgentSession(BaseModel):
    """One question, driven to completion (or the iteration bound)."""

    answer: str
    stop_reason: str | None
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    wall_s: float

    @property
    def hit_iteration_bound(self) -> bool:
        """True when the loop ended on our bound, not the model's ``end_turn``."""
        return self.stop_reason == "tool_use"


class KbAgent:
    """A knowledge-base-grounded agent: one tool, model-driven retrieval."""

    def __init__(
        self,
        kb: KnowledgeBaseClient,
        *,
        model_id: str = DEFAULT_AGENT_MODEL_ID,
        bedrock_client: Any = None,
        system: str = HEALTH_ASSISTANT_SYSTEM,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        top_k: int = 5,
        search_type: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.kb = kb
        self.model_id = model_id
        self.max_iterations = max_iterations
        self.top_k = top_k
        self.search_type = search_type
        # Any on purpose: tests inject fakes, and the boto3-stubs TypedDicts
        # would force every literal below into casts for no runtime gain.
        self._bedrock: Any = bedrock_client or boto3.client("bedrock-runtime")
        self._system = system + " Use the search_kb tool for facts."
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_retries = max_retries
        self._sleep = sleep
        self._rng = rng
        self._clock = clock

    def _with_backoff(self, call: Callable[[], Any]) -> Any:
        """Jittered exponential backoff, same shape and rationale as llm.py."""
        for attempt in range(self._max_retries + 1):
            try:
                return call()
            except ClientError as error:
                if not _is_retryable(error) or attempt == self._max_retries:
                    raise
                self._sleep(2**attempt + self._rng())
        raise RuntimeError("unreachable")

    def _render(self, passages: list[RetrievedPassage]) -> str:
        """What the model reads back. 600 chars/passage: enough to ground an
        answer, small enough that the stateless re-pay tax (every prior tool
        result rides along on every later call) stays affordable."""
        if not passages:
            return "No passages found."
        return "\n\n".join(f"[{p.citation}]\n{p.text[:600]}" for p in passages)

    def ask(self, question: str) -> AgentSession:
        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": question}]}]
        tool_calls: list[ToolCall] = []
        tokens_in = tokens_out = 0
        stop: str | None = None
        answer = ""
        started = self._clock()

        for _ in range(self.max_iterations):
            response = self._with_backoff(
                lambda: self._bedrock.converse(
                    modelId=self.model_id,
                    system=[{"text": self._system}],
                    messages=messages,
                    toolConfig=_TOOL_CONFIG,
                    inferenceConfig={
                        "maxTokens": self._max_tokens,
                        "temperature": self._temperature,
                    },
                )
            )
            usage = response.get("usage", {})
            tokens_in += usage.get("inputTokens", 0)
            tokens_out += usage.get("outputTokens", 0)
            stop = response.get("stopReason")
            output_message = response["output"]["message"]
            if stop != "tool_use":
                answer = extract_answer_text(response)
                break

            messages.append({"role": "assistant", "content": output_message["content"]})
            tool_results: list[dict[str, Any]] = []
            for block in output_message["content"]:
                if "toolUse" not in block:
                    continue
                tool_use = block["toolUse"]
                query = tool_use["input"]["query"]
                floor = tool_use["input"].get("min_evidence")
                passages = self._with_backoff(
                    lambda query=query, floor=floor: self.kb.retrieve(  # type: ignore[misc]
                        query,
                        top_k=self.top_k,
                        min_evidence=floor,
                        rerank=False,
                        search_type=self.search_type,
                    )
                )
                tool_calls.append(ToolCall(query=query, min_evidence=floor, passages=passages))
                tool_results.append(
                    {
                        "toolResult": {
                            "toolUseId": tool_use["toolUseId"],
                            "content": [{"text": self._render(passages)}],
                        }
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        return AgentSession(
            answer=answer,
            stop_reason=stop,
            tool_calls=tool_calls,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            wall_s=round(self._clock() - started, 2),
        )
