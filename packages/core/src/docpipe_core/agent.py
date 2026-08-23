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

from docpipe_core.aws import is_retryable
from docpipe_core.llm import HEALTH_ASSISTANT_SYSTEM, extract_answer_text
from docpipe_core.retrieval import KnowledgeBaseClient, RetrievedPassage

DEFAULT_AGENT_MODEL_ID = "deepseek.v3.2"  # ON_DEMAND + tool use; R1 has neither
DEFAULT_MAX_ITERATIONS = 5

SEARCH_TOOL_NAME = "search_kb"

# The seeded first call's toolUseId. Client-chosen ids are legal in Converse
# history — the API only requires every toolUse to be answered by a matching
# toolResult, which the seed pair satisfies.
VERBATIM_SEED_TOOL_USE_ID = "verbatim-seed"

_VERBATIM_SEED_SYSTEM_NOTE = (
    " The knowledge base has already been searched with the user's question "
    "verbatim (the first tool call above); search again only with reformulated "
    "or narrowed queries."
)

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

    @property
    def guardrail_intervened(self) -> bool:
        """True when the guardrail replaced the answer with its own refusal.

        Derived from ``stop_reason`` rather than stored, so it cannot drift out
        of step with it. A caller that logs or scores answers needs to tell a
        refusal apart from a reply — they are both non-empty strings, and the
        guardrail's text reads like a perfectly ordinary answer.
        """
        return self.stop_reason == "guardrail_intervened"


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
        guardrail_id: str | None = None,
        guardrail_version: str = "DRAFT",
        verbatim_first: bool = False,
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
        self.verbatim_first = verbatim_first
        # Any on purpose: tests inject fakes, and the boto3-stubs TypedDicts
        # would force every literal below into casts for no runtime gain.
        self._bedrock: Any = bedrock_client or boto3.client("bedrock-runtime")
        self._system = system + " Use the search_kb tool for facts."
        if verbatim_first:
            self._system += _VERBATIM_SEED_SYSTEM_NOTE
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._guardrail_id = guardrail_id
        self._guardrail_version = guardrail_version
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
                if not is_retryable(error) or attempt == self._max_retries:
                    raise
                self._sleep(2**attempt + self._rng())
        raise RuntimeError("unreachable")

    def _guardrail_kwargs(self) -> dict[str, Any]:
        """``guardrailConfig``, or nothing when no guardrail is configured.

        A provisioned guardrail enforces nothing on its own — it applies only to
        calls that name it. `llm.py` has passed this since the guardrail was
        built; this loop did not, so **the agentic path — the best-performing
        path, and the one a chat would ship — ran unguarded from the day it was
        written until 2026-08-23.** The non-diagnostic stance rested entirely on
        a system prompt, which prompt injection is free to argue with.

        Applied on EVERY iteration, not just the last. The model can emit text
        on a tool-use turn, and an intervention has to be able to stop the loop
        before another retrieval round, not after the final answer is composed.

        The question of whether to ``guardContent``-tag got its measured
        answer on 2026-08-24 (scripts/guardrail_probe.py): untagged, every
        toolResult turn re-enters the *input* assessment, and the corpus's own
        text is what trips it — the referral chunk via the MedicalDiagnosis
        topic, a mechanism chunk via PROMPT_ATTACK at LOW confidence — while
        the bare questions almost never do (3/66, deterministic). So
        ``_question_block`` tags the user's question; see its docstring. The
        role needs `bedrock:ApplyGuardrail`.
        """
        if not self._guardrail_id:
            return {}
        return {
            "guardrailConfig": {
                "guardrailIdentifier": self._guardrail_id,
                "guardrailVersion": self._guardrail_version,
            }
        }

    def _question_block(self, question: str) -> dict[str, Any]:
        """The user's question — ``guardContent``-tagged when a guardrail is on.

        Converse semantics: with no ``guardContent`` block in the request, the
        guardrail input-assesses ALL user content — which, in a tool loop,
        includes every retrieved passage riding back in a toolResult turn.
        Measured 2026-08-24: that is where the blocking actually came from.
        The 2026-08-23 re-baseline's stochastic 31/61 ever-blocked pool was a
        lottery over *what got retrieved*, not over the questions — bare
        questions block 3/66 and deterministically. With this tag present the
        guardrail input-assesses only the question; model output is still
        assessed on every iteration, so the topic policy still applies to what
        the assistant actually says.

        Plain text when no guardrail is configured — ``guardContent`` without a
        ``guardrailConfig`` is a validation error.
        """
        if not self._guardrail_id:
            return {"text": question}
        return {"guardContent": {"text": {"text": question}}}

    def _seed_verbatim_search(
        self,
        question: str,
        messages: list[dict[str, Any]],
        tool_calls: list[ToolCall],
    ) -> None:
        """Make tool call 1 the user's question verbatim, before the model runs.

        Why (FINDINGS six-run entry, 2026-08-20): single-shot ∪ agentic covered
        61/61 answerable questions while neither strategy alone did — the
        model's reformulations sometimes lose exactly what embedding the
        verbatim question finds. Prompting the model to search verbatim first
        would leave that to compliance; seeding the call makes it a protocol
        fact. The seed rides into history as an ordinary toolUse/toolResult
        pair, counts as a session tool call for scoring, and the system prompt
        tells the model it already happened so it reformulates instead of
        repeating.

        One semantic this changes on the guarded path: the seed retrieval runs
        *before* any Converse call, so a question the guardrail would block at
        input still performs one retrieval. That is deliberate — the eval
        measures retrieval reach — but a shipped flow that must not retrieve
        for blocked inputs would need the guardrail consulted first.
        """
        passages = self._with_backoff(
            lambda: self.kb.retrieve(
                question,
                top_k=self.top_k,
                min_evidence=None,
                rerank=False,
                search_type=self.search_type,
            )
        )
        tool_calls.append(ToolCall(query=question, min_evidence=None, passages=passages))
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "toolUse": {
                            "toolUseId": VERBATIM_SEED_TOOL_USE_ID,
                            "name": SEARCH_TOOL_NAME,
                            "input": {"query": question},
                        }
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": VERBATIM_SEED_TOOL_USE_ID,
                            "content": [{"text": self._render(passages)}],
                        }
                    }
                ],
            }
        )

    def _render(self, passages: list[RetrievedPassage]) -> str:
        """What the model reads back. 600 chars/passage: enough to ground an
        answer, small enough that the stateless re-pay tax (every prior tool
        result rides along on every later call) stays affordable."""
        if not passages:
            return "No passages found."
        return "\n\n".join(f"[{p.citation}]\n{p.text[:600]}" for p in passages)

    def ask(self, question: str) -> AgentSession:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [self._question_block(question)]}
        ]
        tool_calls: list[ToolCall] = []
        tokens_in = tokens_out = 0
        stop: str | None = None
        answer = ""
        started = self._clock()

        if self.verbatim_first:
            self._seed_verbatim_search(question, messages, tool_calls)

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
                    **self._guardrail_kwargs(),
                )
            )
            usage = response.get("usage", {})
            tokens_in += usage.get("inputTokens", 0)
            tokens_out += usage.get("outputTokens", 0)
            stop = response.get("stopReason")
            # `output.message` is read only once we know we are continuing.
            # A guardrail intervention is a stop reason this loop never saw
            # before, and indexing into a response shape we have not observed
            # would turn a refusal — the safe outcome — into a KeyError.
            if stop != "tool_use":
                answer = extract_answer_text(response)
                break
            output_message = response["output"]["message"]

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
