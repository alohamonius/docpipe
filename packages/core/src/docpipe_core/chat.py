"""One ``POST /chat`` turn, end to end: history → ``KbAgent`` → persist → reply.

This is the logic the Lambda in ``services/api/chat.py`` wraps. It lives here,
typed and unit-tested with the same fakes as the agent's protocol tests, because
the service tree is a thin shell by rule (README, repo layout) and a Lambda
handler is the hardest place in the repo to test anything.

The agent is ``KbAgent`` — the repo's own Converse tool loop — not a Strands
agent, although PLAN.md's Phase 3 checklist named one in August. ``agent.py``'s
docstring has called itself "Phase 3's shipped agent" since 2026-08-21, and
every measured number behind this endpoint (0.95 session recall, the guardrail's
cost, the verbatim seed's +4, the ``guardContent`` fix of 2026-08-24) was
measured on it. A framework loop would re-derive the guardrail wiring from
scratch and would not tag only the question — which re-opens the corpus-blocks-
itself failure the probe found. Lib-vs-hand-rolled is a HOW choice; this is the
measured one.

Three contract points a reader should know without opening the tests:

* **Citations are the retrieved passages**, deduplicated by ``source_path``,
  across every ``search_kb`` call of the turn — the model does not report which
  it relied on, and inventing that would be a second model call. Each carries
  the five stamp fields health.studio's verifier reads (``source_path``,
  ``doc_title``, ``section``, ``max_evidence``, ``verification``).
* **A backend failure persists nothing.** ``ModelInvocationError`` (the handler's
  502) is raised before ``append``; the conversation reads exactly as it did
  before the request. A guardrail intervention is *not* a failure: the refusal is
  what the user saw, so it is stored and flagged.
* **An unknown ``conversationId`` restarts under that id.** DynamoDB's TTL can
  delete a conversation mid-thread; a client that keeps sending the id gets a
  fresh history, not a 404 it cannot recover from. Ids are scoped per user by
  the table's key, so one user cannot read another's by guessing.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping

from botocore.exceptions import ClientError
from pydantic import AliasChoices, BaseModel, Field

from docpipe_core.agent import DEFAULT_AGENT_MODEL_ID, DEFAULT_MAX_ITERATIONS, AgentSession, KbAgent
from docpipe_core.conversations import DEFAULT_TTL_DAYS, ConversationStore
from docpipe_core.llm import HEALTH_ASSISTANT_SYSTEM, ModelInvocationError
from docpipe_core.models import ChatMessage, new_conversation_id
from docpipe_core.retrieval import KnowledgeBaseClient, RetrievedPassage

# The phrase a reply opens with when the corpus does not cover the question.
# A protocol fact rather than a vibe: scripts/chat_smoke.py asserts it on an
# out-of-corpus question, so the "say so instead of guessing" rule has a test.
NOT_COVERED_MARKER = "Not covered by the knowledge base"

CHAT_SYSTEM = HEALTH_ASSISTANT_SYSTEM + (
    f' If the knowledge base does not cover the question, begin your reply with "'
    f'{NOT_COVERED_MARKER}." and say what you can and cannot answer — never guess.'
)

# What a user id must look like in anything this public repo commits or any
# smoke run sends: eight hex characters after `smoke-`. health.studio's real ids
# are opaque but they are still user data, and a fixture with a real one would
# publish it. scripts/chat_smoke.py refuses anything else by this pattern.
SYNTHETIC_USER_ID = re.compile(r"^smoke-[a-f0-9]{8}$")

# Prior messages replayed to the model: five turns. Bounded because every prior
# message rides along on every Converse call of the loop (the stateless re-pay
# tax agent.py describes), and a chat about one body region rarely needs more.
DEFAULT_HISTORY_LIMIT = 10
DEFAULT_TOP_K = 5


class ChatRequest(BaseModel):
    """The wire shape of ``POST /chat``: camelCase on the wire, snake_case here."""

    user_id: str = Field(
        validation_alias=AliasChoices("userId", "user_id"), min_length=1, max_length=128
    )
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("conversationId", "conversation_id"),
        min_length=1,
        max_length=128,
    )


class Citation(BaseModel):
    """One retrieved passage's attribution — the five stamp fields, plus a label."""

    source_path: str | None
    doc_title: str | None
    section: str | None
    max_evidence: int | None
    verification: list[str] | None
    label: str

    @classmethod
    def of(cls, passage: RetrievedPassage) -> Citation:
        return cls(
            source_path=passage.source_path,
            doc_title=passage.doc_title,
            section=passage.section,
            max_evidence=passage.max_evidence,
            verification=passage.verification,
            label=passage.citation,
        )


class ChatResponse(BaseModel):
    """The wire shape of a reply (``model_dump(by_alias=True)``). These names are
    the contract health.studio reads in 05; keep them."""

    reply: str
    citations: list[Citation]
    conversation_id: str = Field(
        serialization_alias="conversationId",
        validation_alias=AliasChoices("conversationId", "conversation_id"),
    )
    model_id: str
    guardrail_intervened: bool
    latency_ms: int
    input_tokens: int
    output_tokens: int
    tool_calls: int


class ChatService:
    """History → agent → persist → response. One instance per Lambda container."""

    def __init__(
        self,
        agent: KbAgent,
        store: ConversationStore,
        *,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.agent = agent
        self.store = store
        self._history_limit = history_limit
        self._clock = clock

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> ChatService:
        """Build from the Lambda's environment — the names ``infra/components/api.py`` sets.

        Everything about the shipped configuration is decided here, once:
        the chat model (the stack's ``chat_model_id``, never a code default
        when the stack has spoken), the guardrail, and the verbatim seed —
        on, because it is the measured-best configuration and because it
        guarantees at least one retrieval per turn, so a reply always has
        citations to show or an honest "not covered" to say.
        """
        kb = KnowledgeBaseClient(_required(env, "KNOWLEDGE_BASE_ID"))
        agent = KbAgent(
            kb,
            model_id=env.get("CHAT_MODEL_ID") or DEFAULT_AGENT_MODEL_ID,
            system=CHAT_SYSTEM,
            guardrail_id=env.get("GUARDRAIL_ID") or None,
            guardrail_version=env.get("GUARDRAIL_VERSION") or "DRAFT",
            verbatim_first=True,
            max_iterations=int(env.get("CHAT_MAX_ITERATIONS") or DEFAULT_MAX_ITERATIONS),
            top_k=int(env.get("CHAT_TOP_K") or DEFAULT_TOP_K),
        )
        store = ConversationStore(
            _required(env, "CONVERSATIONS_TABLE"),
            ttl_days=int(env.get("CONVERSATION_TTL_DAYS") or DEFAULT_TTL_DAYS),
        )
        return cls(
            agent,
            store,
            history_limit=int(env.get("CHAT_HISTORY_LIMIT") or DEFAULT_HISTORY_LIMIT),
        )

    def turn(self, request: ChatRequest) -> ChatResponse:
        started = self._clock()
        conversation_id = request.conversation_id or new_conversation_id()
        history = self._history(request.user_id, request.conversation_id)
        try:
            session = self.agent.ask(request.message, history=history)
        except ClientError as error:
            # Retrieve or Converse, after the agent's own retries. Nothing is
            # persisted: the conversation reads as it did before the request.
            code = error.response.get("Error", {}).get("Code", "")
            raise ModelInvocationError(f"Bedrock call failed: {code}") from error
        if not session.answer:
            raise ModelInvocationError(
                f"the agent stopped without an answer (stop_reason={session.stop_reason!r})"
            )
        self.store.append(
            request.user_id,
            conversation_id,
            [
                ChatMessage(role="user", content=request.message),
                ChatMessage(role="assistant", content=session.answer),
            ],
        )
        return ChatResponse(
            reply=session.answer,
            citations=_citations_of(session),
            conversation_id=conversation_id,
            model_id=self.agent.model_id,
            guardrail_intervened=session.guardrail_intervened,
            latency_ms=int((self._clock() - started) * 1000),
            input_tokens=session.input_tokens,
            output_tokens=session.output_tokens,
            tool_calls=len(session.tool_calls),
        )

    def _history(self, user_id: str, conversation_id: str | None) -> list[ChatMessage]:
        if conversation_id is None:
            return []
        conversation = self.store.get(user_id, conversation_id)
        if conversation is None:
            return []
        messages = conversation.messages[-self._history_limit :]
        # Whole turns only: Converse alternates strictly and the first replayed
        # message must be the user's, so an odd cap drops the dangling answer.
        if messages and messages[0].role != "user":
            messages = messages[1:]
        return messages


def _citations_of(session: AgentSession) -> list[Citation]:
    seen: set[str] = set()
    citations: list[Citation] = []
    for call in session.tool_calls:
        for passage in call.passages:
            key = passage.source_path or passage.source or passage.text[:80]
            if key in seen:
                continue
            seen.add(key)
            citations.append(Citation.of(passage))
    return citations


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set — infra/components/api.py sets it on the Lambda")
    return value


__all__: list[str] = [
    "CHAT_SYSTEM",
    "DEFAULT_HISTORY_LIMIT",
    "NOT_COVERED_MARKER",
    "SYNTHETIC_USER_ID",
    "ChatRequest",
    "ChatResponse",
    "ChatService",
    "Citation",
]
