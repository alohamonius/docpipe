"""Score the knowledge base against a held-out question set.

WHY THIS EXISTS. Every proposal downstream of the corpus — splitting oversized
chunks, moving the bibliography out of the embedded payload, carrying source
trails through to the structure nodes — is currently argued from plausibility.
"A real quality win" is a claim with no way to confirm *or refute* it. This
module is the refutation path: a fixed set of questions, a fixed answer key,
and a score with an ``n`` attached, so each of those changes reports a signed
delta instead of an opinion.

WHY IT USES ``KnowledgeBaseClient`` AND NOT ITS OWN QUERY. The metric has to be
taken through the same call the product makes, or the thing measured stops
being the thing shipped. So this scores ``retrieve()`` — same client, same
retrieval filter semantics, same passage parsing. A harness with a private
query path can score a system nobody runs.

THREE METRICS, AND WHY THE THIRD ONE IS HERE.

* ``recall@k`` — did any correct chunk come back at all.
* ``MRR`` — how far up. A correct chunk at rank 5 is not the same answer as a
  correct chunk at rank 1, and a reshape that only moves ranks would read as
  flat under recall alone.
* ``stamp integrity`` — the fraction of returned passages that still carry a
  complete evidence header. This corpus stamps the ★ scale, the
  [VERIFIED]/[UNVERIFIED]/[SYNTHESIS] vocabulary and the "not a diagnosis"
  line onto every chunk precisely so a retrieved fragment cannot be read as
  bare authority. Any splitter keeps that header on the first fragment and
  strips it from the rest — so a chunking regression shows up here *first*,
  and as a safety number rather than a quality one. 100% is the only passing
  value; the corpus itself is at 382/383 on disk, which is the ceiling.

SCORED PER STRATUM, NEVER AS ONE AVERAGE. The corpus is 192 prose chunks
against 132 connection chunks, 31 near-empty structure chunks and singleton
joint/index chunks. A change that helps prose and hurts the graph half reads
as neutral in one number, and the graph half only exists because the corpus
builder ships it.

THE FOURTH THING, WHICH IS NOT A RETRIEVAL METRIC. Roughly 91 registry rows
are unresolved, and an unresolved row has no node — so it is *absent* from the
corpus rather than reported as excluded. Asked about one, retrieval has
nothing to return and will hand back the nearest neighbour. ``abstention``
measures that hole directly: for a question the KB cannot answer, did the
named confabulation magnets stay out of the top k. It is scored separately
because it is not recall — there is no correct chunk to find.

No AWS in this module: the client is injected, so the whole thing is unit-
tested against a fake runtime.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .kb_sync import DEFAULT_PREFIX
from .retrieval import DEFAULT_RERANK_POOL, KnowledgeBaseClient, RetrievedPassage

# ── The stamp, as `stamp.ts` writes it ─────────────────────────────────────
#
# Kept as literal fragments rather than a regex: this is a cross-repo contract
# (health.studio `apps/web/src/lib/kb/stamp.ts` is the writer) and a literal
# that stops matching is a legible failure, where a clever pattern that
# silently keeps matching a degraded header is not.

STAMP_PROVENANCE = "<!-- health.studio knowledge base"
STAMP_SOURCE_LINE = "> **Source** ·"
STAMP_DISCLAIMER = "> **Use** · Educational. Not a diagnosis"
STAMP_SCALE = "★★★ strong"
RATING_GLYPH = "★"


def stamp_is_complete(text: str) -> bool:
    """Does this passage carry its whole evidence header?

    The scale is only required when the passage actually uses a ★ — the
    stamper is adaptive and gives an unrated chunk no scale, so demanding one
    unconditionally would fail 97 legitimately unrated chunks. What must never
    happen is the other way round: stars present, scale absent. That is what a
    splitter produces, and it reads as authority.
    """
    if STAMP_PROVENANCE not in text:
        return False
    if STAMP_SOURCE_LINE not in text:
        return False
    if STAMP_DISCLAIMER not in text:
        return False
    # Stars present, scale absent: the splitter signature.
    return RATING_GLYPH not in text or STAMP_SCALE in text


# ── The question set ───────────────────────────────────────────────────────


class Question(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    stratum: str
    # `class` is a Python keyword; the JSON field keeps the readable name.
    question_class: str = Field(alias="class")
    question: str
    expected: list[str]
    must_not_return: list[str] = Field(default_factory=list)
    why: str = ""


class QuestionSet(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    version: int
    authored: str
    ratified: str | None = None
    ratified_by: str | None = None
    questions: list[Question]

    @property
    def is_ratified(self) -> bool:
        return bool(self.ratified)


def load_question_set(path: Path | str) -> QuestionSet:
    return QuestionSet.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


class UnratifiedAnswerKey(RuntimeError):
    """Raised when a scoring run is asked to treat a self-graded key as truth.

    An agent that writes both the questions and the correct answers has graded
    its own homework. The gate is a human confirming, per question, that the
    named chunk really is the right one — so a score taken before that is a
    number, not a baseline, and this refuses to let one be recorded as one.
    """


# ── Mapping a returned passage back to a chunk key ─────────────────────────


def chunk_key_of(passage: RetrievedPassage, prefix: str = DEFAULT_PREFIX) -> str | None:
    """`s3://bucket/corpus/anatomy/12-pain-science--05-….md` → `anatomy/…md`.

    Falls back to the ``sourcePath`` sidecar attribute only when the S3 URI is
    absent — and it is *not* interchangeable: ``sourcePath`` names the
    authoring document (`docs/anatomy/12-pain-science.md`), one level above the
    chunk. Scoring on it would grade a document rather than a chunk, which is
    the whole distinction the corpus is built around, so it returns None here
    rather than silently answering a different question.
    """
    if passage.source is None:
        return None
    marker = f"/{prefix}" if not prefix.startswith("/") else prefix
    at = passage.source.find(marker)
    if at == -1:
        return None
    return passage.source[at + len(marker) :] or None


# ── The score ──────────────────────────────────────────────────────────────


class QuestionResult(BaseModel):
    id: str
    stratum: str
    question_class: str
    hit: bool
    rank: int | None  # 1-based rank of the first expected chunk; None = miss
    reciprocal_rank: float
    returned: list[str]
    forbidden_returned: list[str]
    passages: int
    stamped: int

    @property
    def abstained(self) -> bool:
        """For a not-covered question: did every confabulation magnet stay out."""
        return not self.forbidden_returned


class StratumScore(BaseModel):
    stratum: str
    n: int
    recall_at_k: float
    mrr: float
    stamp_integrity: float
    passages: int
    stamped: int
    abstention: float | None = None  # not-covered questions only
    misses: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    """One scored run. Serialises straight into `corpus-census.json` `scores[]`."""

    model_config = ConfigDict(extra="forbid")

    k: int
    n: int
    min_evidence: int | None
    # Recorded so a report is self-describing: a reranked score and a raw one
    # are different experiments, and a number that doesn't say which it is
    # cannot be compared against the baseline. Defaults keep the committed
    # 2026-08-16 baseline (written before these fields existed) loadable as-is.
    rerank: bool = False
    rerank_pool: int | None = None
    knowledge_base_id: str
    question_set: dict[str, Any]
    overall: StratumScore
    by_stratum: list[StratumScore]
    results: list[QuestionResult]
    forbidden_violations: list[str]


def _stratum_score(name: str, results: list[QuestionResult]) -> StratumScore:
    n = len(results)
    passages = sum(r.passages for r in results)
    stamped = sum(r.stamped for r in results)
    gap = [r for r in results if r.question_class == "not-covered"]
    return StratumScore(
        stratum=name,
        n=n,
        recall_at_k=round(sum(1 for r in results if r.hit) / n, 4) if n else 0.0,
        mrr=round(sum(r.reciprocal_rank for r in results) / n, 4) if n else 0.0,
        # No returned passages at all is 0.0, not 1.0 — an empty result set has
        # not "kept every stamp intact", it has failed to answer.
        stamp_integrity=round(stamped / passages, 4) if passages else 0.0,
        passages=passages,
        stamped=stamped,
        abstention=(round(sum(1 for r in gap if r.abstained) / len(gap), 4) if gap else None),
        misses=[r.id for r in results if not r.hit],
    )


def score_question_set(
    client: KnowledgeBaseClient,
    question_set: QuestionSet,
    *,
    k: int = 5,
    min_evidence: int | None = None,
    rerank: bool = False,
    rerank_pool: int = DEFAULT_RERANK_POOL,
    prefix: str = DEFAULT_PREFIX,
    require_ratified: bool = True,
) -> EvalReport:
    """Run every question through the shipped retrieval path and score it.

    ``require_ratified`` defaults to True. Pass False only to exercise the
    harness itself — the number it produces is a dry run, not a baseline.

    **``rerank`` defaults False here even though ``retrieve()`` now defaults
    True, and the divergence is deliberate.** This harness is the instrument
    that measured the flip; if its default tracked the library's, re-running the
    2026-08-16 baseline would silently score the reranked path and the row it
    is compared against would stop meaning what it says. Every mode is passed
    explicitly and recorded on the report (``EvalReport.rerank`` /
    ``rerank_pool``), so a stored score is self-describing. Change this default
    and the raw baseline becomes unreproducible.
    """
    if require_ratified and not question_set.is_ratified:
        raise UnratifiedAnswerKey(
            "the answer key has not been ratified by a human; "
            "score with require_ratified=False for a dry run, but do not record it as a baseline"
        )

    results: list[QuestionResult] = []
    for question in question_set.questions:
        passages = client.retrieve(
            question.question,
            top_k=k,
            min_evidence=min_evidence,
            rerank=rerank,
            rerank_pool=rerank_pool,
        )
        keys = [chunk_key_of(p, prefix) for p in passages]
        returned = [key for key in keys if key is not None]

        rank: int | None = None
        for position, key in enumerate(keys, start=1):
            if key is not None and key in question.expected:
                rank = position
                break

        forbidden = [key for key in returned if key in question.must_not_return]

        results.append(
            QuestionResult(
                id=question.id,
                stratum=question.stratum,
                question_class=question.question_class,
                hit=rank is not None,
                rank=rank,
                reciprocal_rank=round(1 / rank, 4) if rank else 0.0,
                returned=returned,
                forbidden_returned=forbidden,
                passages=len(passages),
                stamped=sum(1 for p in passages if stamp_is_complete(p.text)),
            )
        )

    grouped: dict[str, list[QuestionResult]] = defaultdict(list)
    for result in results:
        grouped[result.stratum].append(result)

    return EvalReport(
        k=k,
        n=len(results),
        min_evidence=min_evidence,
        rerank=rerank,
        rerank_pool=rerank_pool if rerank else None,
        knowledge_base_id=client.knowledge_base_id,
        question_set={
            "version": question_set.version,
            "authored": question_set.authored,
            "ratified": question_set.ratified,
            "ratified_by": question_set.ratified_by,
        },
        overall=_stratum_score("ALL", results),
        by_stratum=[_stratum_score(name, grouped[name]) for name in sorted(grouped)],
        results=results,
        # Hoisted to the top level because a forbidden chunk coming back is not
        # a scoring detail — on `gap-04` it is the system answering a question
        # about chest pain with a gallbladder red flag.
        forbidden_violations=[r.id for r in results if r.forbidden_returned],
    )
