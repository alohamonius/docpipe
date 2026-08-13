"""Knowledge-base retrieval via Bedrock Knowledge Bases.

The KB is built from health.studio's public, evidence-graded content, synced by
``kb_sync`` as a chunk-final corpus: one S3 object per retrievable unit, each
with a ``.metadata.json`` sidecar. Bedrock attaches those attributes to every
vector, and they come back on each retrieval result — so a passage knows its
document title, its section, and **how well evidenced it is**.

Why the grading matters here. The corpus is deliberately honest about its own
weak spots: of 383 chunks, 97 are ``maxEvidence: 0`` and 172 carry no citation
at all. Framing prose and conceptual models are not errors, but they retrieve
identically to a systematic review unless something separates them. Two hooks
do that: ``RetrievedPassage.max_evidence`` (so a reply can say which it is) and
``retrieve(min_evidence=…)`` (so a caller can refuse the unrated ones outright).

``min_evidence`` defaults to ``None`` — nothing is filtered unless asked. The
health.studio code graph enforces a floor of 1 per-edge for its pain-reasoning
path; mirroring that here is the caller's decision, not this module's.
"""

from __future__ import annotations

from typing import Any

import boto3
from pydantic import BaseModel

# Attribute names written by health.studio's `pnpm kb:build` into each
# `<chunk>.md.metadata.json`. Bedrock echoes them back under `metadata`.
_DOC_TITLE = "docTitle"
_SECTION = "section"
_SOURCE_PATH = "sourcePath"
_MAX_EVIDENCE = "maxEvidence"
_VERIFICATION = "verification"
_CITATION_COUNT = "citationCount"
_SAFETY_CRITICAL = "safetyCritical"


class RetrievedPassage(BaseModel):
    text: str
    source: str | None = None
    score: float | None = None

    # From the sidecar. All optional: a corpus synced before sidecars existed —
    # or any non-health.studio data source — simply leaves them None.
    doc_title: str | None = None
    section: str | None = None
    source_path: str | None = None
    max_evidence: int | None = None
    verification: list[str] | None = None
    citation_count: int | None = None
    safety_critical: bool | None = None

    @property
    def citation(self) -> str:
        """Human-readable attribution: 'Pain Science → Central sensitization (★★★)'.

        Falls back through source_path → source → the bare text when the
        sidecar attributes are absent, so this never returns an empty string.
        """
        title = self.doc_title or self.source_path or self.source
        parts = [p for p in (title, self.section) if p]
        label = " → ".join(parts) if parts else "unattributed passage"
        if self.max_evidence is not None:
            label += f" ({'★' * self.max_evidence}{'☆' * (3 - self.max_evidence)})"
        return label


class KnowledgeBaseClient:
    def __init__(self, knowledge_base_id: str, agent_runtime_client: Any = None) -> None:
        self.knowledge_base_id = knowledge_base_id
        self._client = agent_runtime_client or boto3.client("bedrock-agent-runtime")

    def retrieve(
        self,
        query: str,
        top_k: int = 4,
        *,
        min_evidence: int | None = None,
    ) -> list[RetrievedPassage]:
        """Retrieve passages, optionally refusing anything below an evidence floor.

        ``min_evidence`` maps to a Bedrock ``greaterThanOrEquals`` retrieval
        filter on the ``maxEvidence`` sidecar attribute, so the filtering
        happens in the vector store — an excluded chunk never reaches the model
        and never consumes context. ``None`` (the default) filters nothing.
        """
        search: dict[str, Any] = {"numberOfResults": top_k}
        if min_evidence is not None:
            search["filter"] = {
                "greaterThanOrEquals": {"key": _MAX_EVIDENCE, "value": min_evidence}
            }
        response = self._client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": search},
        )
        return [_passage_of(result) for result in response.get("retrievalResults", [])]


def _passage_of(result: dict[str, Any]) -> RetrievedPassage:
    metadata = result.get("metadata", {})
    return RetrievedPassage(
        text=result.get("content", {}).get("text", ""),
        source=_source_of(result),
        score=result.get("score"),
        doc_title=_as_str(metadata.get(_DOC_TITLE)),
        section=_as_str(metadata.get(_SECTION)),
        source_path=_as_str(metadata.get(_SOURCE_PATH)),
        max_evidence=_as_int(metadata.get(_MAX_EVIDENCE)),
        verification=_as_str_list(metadata.get(_VERIFICATION)),
        citation_count=_as_int(metadata.get(_CITATION_COUNT)),
        safety_critical=_as_bool(metadata.get(_SAFETY_CRITICAL)),
    )


# Bedrock returns sidecar attributes already unwrapped to plain JSON values, but
# the corpus is generated elsewhere and a type drift there should degrade to
# None rather than raise mid-retrieval. Hence the tolerant coercions below.


def _as_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    # bool is an int subclass — exclude it, or safetyCritical=true reads as 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_str_list(value: Any) -> list[str] | None:
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return None


def _source_of(result: dict[str, Any]) -> str | None:
    metadata = result.get("metadata", {})
    if uri := metadata.get("x-amz-bedrock-kb-source-uri"):
        return str(uri)
    location = result.get("location", {})
    for key in ("s3Location", "webLocation"):
        if inner := location.get(key):
            return inner.get("uri") or inner.get("url")
    return None
