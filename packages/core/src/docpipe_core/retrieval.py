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

# The only rerank model Bedrock offers ON_DEMAND in us-east-1 (measured
# 2026-08-16). A cross-encoder: it reads query and passage *together*, so it can
# rank on meaning the bi-encoder embedding blurred — which is exactly the
# corpus's failure mode, where bibliography-heavy graph chunks outrank the right
# answer on embedding distance alone.
RERANK_MODEL_ID = "cohere.rerank-v3-5:0"

# Sized from the 2026-08-16 miss-rank probe (FINDINGS.md): of the 9 baseline
# misses, 7 sat at rank 7–19 on raw embedding distance — inside a pool of 25 —
# and the other 2 were absent even at 25. A pool this size therefore gives the
# reranker every rescuable miss while keeping the per-query rerank cost at one
# 25-document call.
DEFAULT_RERANK_POOL = 25


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
    def __init__(
        self,
        knowledge_base_id: str,
        agent_runtime_client: Any = None,
        *,
        semantic_score_is_distance: bool = False,
    ) -> None:
        """``semantic_score_is_distance``: set True for an Aurora/RDS-backed KB.

        Measured 2026-08-20 (FINDINGS): on the RDS storage path Bedrock returns
        ``score = 1 - cosine`` — the *distance*, exact to six decimals — and
        sorts descending, so the semantic result list arrives **worst-first**
        (63/66 golden questions came back in exactly reversed order; the
        candidate set itself is the correct top-k). The S3 Vectors path returns
        ``(1 + cosine)/2`` and orders correctly, and the HYBRID and rerank
        paths order correctly on both stores — so the flag re-sorts only the
        raw semantic path, ascending. Per-KB because score semantics are a
        property of the storage backend, not of a call.
        """
        self.knowledge_base_id = knowledge_base_id
        self.semantic_score_is_distance = semantic_score_is_distance
        self._client = agent_runtime_client or boto3.client("bedrock-agent-runtime")

    def retrieve(
        self,
        query: str,
        top_k: int = 4,
        *,
        min_evidence: int | None = None,
        rerank: bool = True,
        rerank_pool: int = DEFAULT_RERANK_POOL,
        search_type: str | None = None,
    ) -> list[RetrievedPassage]:
        """Retrieve passages, optionally refusing anything below an evidence floor.

        Reranking widens the vector search to ``rerank_pool`` candidates and has
        Bedrock's ``cohere.rerank-v3-5`` cross-encoder re-read them against the
        query before cutting back to ``top_k`` — one server-side call, same
        response shape, nothing changes for the caller. The pool must be wider
        than ``top_k`` or the reranker can only reorder the same near-misses it
        was meant to rescue; anything at rank > pool never enters the pool and
        cannot be recovered by reranking at all.

        **It is ON by default, ruled 2026-08-17** (how2doo
        ``goals/kb-retrieval-readiness/04-embedding-payload/goal.md``, *HUMAN
        RULING 2026-08-17 (later)*), on a number measured twice against the same
        66-question held-out set and the same live index: recall@5 0.7879 →
        0.8182, MRR 0.5634 → 0.6773, hits at rank 1 29 → 39. The re-run was a
        control (``--rerun-of``) and came back identical to four decimals with an
        identical miss set, so the trade below is stable rather than variance.

        **Two losses ship with it, named and accepted by that ruling**, because a
        default that hides its cost is a default nobody can audit: *biblio-01*
        ("what are the sources behind…") — the cross-encoder matches the topic
        and discards the word *sources*, so it returns five chain chunks and
        drops the bibliography one — and *nerve-01*, which comes back with ulnar
        *territory* rather than the ulnar *nerve*. Both are reproducible.

        **``rerank=False`` is the supported opt-out and it matters
        operationally**: Cohere Rerank 3.5 is **3 on-demand requests per minute
        at ACCOUNT level and not adjustable** (``service-quotas L-11512E58``), so
        the reranked path suits low-volume or self-paced callers. Anything bursty
        must pass ``rerank=False`` or pace itself — 66 queries take ~24 minutes
        reranked. That path is pinned byte-identical by
        ``test_rerank_off_sends_the_pre_rerank_request_shape``.

        Reranking runs under the **KB service role**, not the caller — Bedrock
        assumes ``<prefix>-kb-role`` for the rerank step, so that role needs
        ``bedrock:Rerank`` plus ``InvokeModel`` on the rerank model (see
        ``pulumi/components/kb.py``; a 403 here names the KB role, not you).

        ``min_evidence`` maps to a Bedrock retrieval filter on the
        ``maxEvidence`` sidecar attribute, so the filtering happens in the
        vector store — an excluded chunk never reaches the model and never
        consumes context. ``None`` (the default) filters nothing.

        **The floor is OR'd with the safety pin, and that is not a nicety.**
        This was a flat ``greaterThanOrEquals``, which is wrong in a way that
        surfaces only the first time someone raises the floor to improve
        quality. Measured against health.studio's corpus and its 66-question
        answer key (that repo's ``docs/FINDINGS.md``, 2026-08-16):

        - ``15-medical-red-flags--02-screening-summary-table-machine-readable``
          is ``maxEvidence: 0`` **and** ``safetyCritical: true``. It is the
          machine-readable red-flag screen, and it carries the cardiac row —
          "chest, left arm, or jaw during exertion → EMERGENCY". A flat floor
          of 1 deletes it.
        - 11 of the 66 questions become unanswerable, 7 of them expecting the
          closed-world index chunk. The honesty apparatus is unrated *by
          construction*: "a connection absent from this list is one the system
          does not know" is a claim about the system, so there is no study to
          cite and no star to earn.

        So a flat floor removes the corpus's ability to abstain and its
        red-flag screen while keeping every confident claim — the exact
        inversion of what raising it is meant to achieve. The fix belongs in
        the filter rather than in the grades: re-grading the red flags to clear
        a floor would contradict the safety override, which is right to be
        ungraded.

        A chunk with no ``safetyCritical`` attribute does not match the second
        arm, so nothing outside health.studio's corpus gains an exemption by
        accident.

        ``search_type`` maps to ``overrideSearchType``: ``"HYBRID"`` asks the
        store to blend vector similarity with full-text keyword match,
        ``"SEMANTIC"`` pins vector-only, and ``None`` — the default — sends
        nothing, so the store decides and every pre-existing call stays
        byte-identical. Two facts gate its use (PLAN.md): Aurora provisions a
        GIN ``to_tsvector`` index for exactly this, and whether S3 Vectors
        accepts HYBRID at all is unverified. The knob exists to *measure*
        hybrid per store, not to flip it blind — an unsupported value fails
        here, before a request is spent on it.
        """
        search: dict[str, Any] = {"numberOfResults": max(rerank_pool, top_k) if rerank else top_k}
        if search_type is not None:
            if search_type not in ("SEMANTIC", "HYBRID"):
                raise ValueError(
                    f"search_type must be 'SEMANTIC', 'HYBRID' or None, got {search_type!r}"
                )
            search["overrideSearchType"] = search_type
        if min_evidence is not None:
            search["filter"] = {
                "orAll": [
                    {"greaterThanOrEquals": {"key": _MAX_EVIDENCE, "value": min_evidence}},
                    {"equals": {"key": _SAFETY_CRITICAL, "value": True}},
                ]
            }
        if rerank:
            search["rerankingConfiguration"] = {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {"modelArn": self._rerank_model_arn()},
                    "numberOfRerankedResults": top_k,
                },
            }
        response = self._client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": search},
        )
        passages = [_passage_of(result) for result in response.get("retrievalResults", [])]
        # Only the raw semantic path carries the inverted RDS score (see
        # __init__); reranked lists are ordered by Cohere and HYBRID lists by
        # the store's fusion, both higher-is-better on both backends.
        if self.semantic_score_is_distance and not rerank and search_type != "HYBRID":
            passages.sort(key=lambda p: (p.score is None, p.score if p.score is not None else 0.0))
        return passages

    def _rerank_model_arn(self) -> str:
        # Foundation-model ARNs are region-scoped; take the region from the
        # runtime client so the rerank call can never cross regions from the
        # retrieve it belongs to. Test fakes carry no `meta`, hence the guard.
        region = getattr(getattr(self._client, "meta", None), "region_name", None) or "us-east-1"
        return f"arn:aws:bedrock:{region}::foundation-model/{RERANK_MODEL_ID}"


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
