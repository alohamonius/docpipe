from typing import Any

import pytest
from docpipe_core.llm import HEALTH_ASSISTANT_SYSTEM, ChatClient
from docpipe_core.models import ChatMessage
from docpipe_core.retrieval import (
    DEFAULT_RERANK_POOL,
    RERANK_MODEL_ID,
    KnowledgeBaseClient,
    RetrievedPassage,
)
from helpers import FakeBedrock, converse_response


class FakeAgentRuntime:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"retrievalResults": self.results}


def kb_result(text: str, uri: str | None = None, score: float = 0.9) -> dict[str, Any]:
    result: dict[str, Any] = {"content": {"text": text}, "score": score}
    if uri:
        result["metadata"] = {"x-amz-bedrock-kb-source-uri": uri}
    return result


def test_retrieve_returns_passages_with_sources() -> None:
    fake = FakeAgentRuntime(
        [
            kb_result("Hamstrings cross two joints.", "s3://kb/anatomy/hamstrings.md"),
            kb_result("Eccentric loading aids tendon repair."),
        ]
    )
    client = KnowledgeBaseClient("kb-123", agent_runtime_client=fake)

    passages = client.retrieve("hamstring rehab", top_k=2)

    assert len(passages) == 2
    assert passages[0].source == "s3://kb/anatomy/hamstrings.md"
    assert passages[1].source is None
    call = fake.calls[0]
    assert call["knowledgeBaseId"] == "kb-123"
    assert call["retrievalQuery"] == {"text": "hamstring rehab"}
    # ``top_k`` is still honoured on the default path — since the 2026-08-17
    # adoption it is honoured by the rerank cut-back rather than by the vector
    # search, which now fetches the wider candidate pool the reranker reads.
    search = call["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert search["numberOfResults"] == DEFAULT_RERANK_POOL
    assert (
        search["rerankingConfiguration"]["bedrockRerankingConfiguration"]["numberOfRerankedResults"]
        == 2
    )


def test_source_from_s3_location_fallback() -> None:
    fake = FakeAgentRuntime(
        [
            {
                "content": {"text": "text"},
                "location": {"s3Location": {"uri": "s3://kb/doc.md"}},
            }
        ]
    )
    passages = KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q")
    assert passages[0].source == "s3://kb/doc.md"


def test_chat_grounding_injects_passages_and_citation_rules() -> None:
    fake = FakeBedrock([converse_response("Per the anatomy notes, yes.")])
    client = ChatClient(bedrock_client=fake)
    passages = [
        RetrievedPassage(text="Hamstrings cross two joints.", source="s3://kb/hamstrings.md")
    ]

    client.reply([ChatMessage(role="user", content="Do hamstrings...?")], passages=passages)

    system_text = fake.calls[0]["system"][0]["text"]
    assert system_text.startswith(HEALTH_ASSISTANT_SYSTEM)
    assert "instead of inventing facts" in system_text
    assert "[1] (source: s3://kb/hamstrings.md) Hamstrings cross two joints." in system_text


def test_chat_without_passages_keeps_plain_system() -> None:
    fake = FakeBedrock([converse_response("ok")])
    ChatClient(bedrock_client=fake).reply([ChatMessage(role="user", content="hi")])
    assert fake.calls[0]["system"] == [{"text": HEALTH_ASSISTANT_SYSTEM}]


# ── Sidecar metadata ───────────────────────────────────────────────────────
# health.studio stamps every chunk with its evidence grade. These pin that the
# grade survives the trip back, because the whole point of the corpus being
# honest about its weak claims is lost if retrieval discards the labels.

SIDECAR = {
    "docTitle": "Pain Science",
    "section": "Central sensitization",
    "sourcePath": "docs/anatomy/12-pain-science.md",
    "maxEvidence": 3,
    "verification": ["VERIFIED"],
    "citationCount": 14,
    "safetyCritical": False,
}


def test_sidecar_attributes_land_on_the_passage() -> None:
    fake = FakeAgentRuntime([{"content": {"text": "Pain is not damage."}, "metadata": SIDECAR}])

    passage = KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("pain")[0]

    assert passage.doc_title == "Pain Science"
    assert passage.section == "Central sensitization"
    assert passage.max_evidence == 3
    assert passage.verification == ["VERIFIED"]
    assert passage.citation_count == 14
    assert passage.safety_critical is False
    assert passage.citation == "Pain Science → Central sensitization (★★★)"


def test_passage_without_sidecar_degrades_to_none_not_error() -> None:
    """A corpus synced before sidecars existed must still retrieve."""
    fake = FakeAgentRuntime([kb_result("bare text", "s3://kb/doc.md")])

    passage = KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q")[0]

    assert passage.max_evidence is None
    assert passage.doc_title is None
    assert passage.citation == "s3://kb/doc.md"


def test_min_evidence_becomes_a_bedrock_filter() -> None:
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", min_evidence=1)

    search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert search["filter"] == {
        "orAll": [
            {"greaterThanOrEquals": {"key": "maxEvidence", "value": 1}},
            {"equals": {"key": "safetyCritical", "value": True}},
        ]
    }


def test_the_evidence_floor_never_filters_out_a_safety_critical_chunk() -> None:
    """The assertion this module exists to keep.

    health.studio's machine-readable red-flag screen is ``maxEvidence: 0`` and
    ``safetyCritical: true`` — it carries the cardiac row routing chest/left-arm
    pain to the ER, and a red-flag screen has no trial to cite, so it can never
    earn a star. Under a flat ``greaterThanOrEquals`` floor it is the FIRST
    thing deleted, and the caller asking for it is someone trying to raise
    quality.

    Asserted at every floor, not just 1: a bare ``>= 3`` would pass a test
    written only against 1 while still dropping the screen.
    """
    for floor in (1, 2, 3):
        fake = FakeAgentRuntime([])
        KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", min_evidence=floor)

        search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
        arms = search["filter"]["orAll"]
        assert {"equals": {"key": "safetyCritical", "value": True}} in arms, (
            f"min_evidence={floor} sends a filter with no safety exemption — "
            "the red-flag screen would be dropped"
        )


def test_the_floor_still_applies_to_everything_else() -> None:
    """The exemption is one arm, not a bypass — the floor is still requested."""
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", min_evidence=2)

    arms = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"]["orAll"]
    assert {"greaterThanOrEquals": {"key": "maxEvidence", "value": 2}} in arms
    assert len(arms) == 2


def test_no_filter_is_sent_by_default() -> None:
    """Default is unfiltered — nothing disappears unless a caller asks."""
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q")

    assert "filter" not in fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]


def test_safety_critical_true_does_not_read_as_evidence_one() -> None:
    """bool is an int subclass — a naive coercion turns True into ★☆☆."""
    fake = FakeAgentRuntime([{"content": {"text": "red flags"}, "metadata": {"maxEvidence": True}}])

    passage = KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q")[0]

    assert passage.max_evidence is None


# ── Reranking ──────────────────────────────────────────────────────────────


def test_rerank_widens_the_pool_and_cuts_back_to_top_k() -> None:
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve(
        "deep buttock pain", top_k=5, rerank=True
    )

    search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert search["numberOfResults"] == DEFAULT_RERANK_POOL
    rerank = search["rerankingConfiguration"]
    assert rerank["type"] == "BEDROCK_RERANKING_MODEL"
    inner = rerank["bedrockRerankingConfiguration"]
    assert inner["numberOfRerankedResults"] == 5
    assert inner["modelConfiguration"]["modelArn"].endswith(RERANK_MODEL_ID)


def test_rerank_is_on_by_default() -> None:
    """Adopted 2026-08-17: a caller who asks for nothing gets the reranked path.

    The ruling that flipped this (how2doo
    ``goals/kb-retrieval-readiness/04-embedding-payload/goal.md``, *HUMAN RULING
    2026-08-17 (later)*) bought recall@5 0.7879 → 0.8182 and MRR 0.5634 →
    0.6773, replicated twice, against two named and accepted losses. The default
    is the whole decision, so it is asserted rather than left to the signature.
    """
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", top_k=4)

    search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert "rerankingConfiguration" in search, (
        "the default retrieve() no longer reranks — that is the 2026-08-17 "
        "adoption ruling being reverted by accident"
    )
    assert search["numberOfResults"] == DEFAULT_RERANK_POOL
    inner = search["rerankingConfiguration"]["bedrockRerankingConfiguration"]
    assert inner["numberOfRerankedResults"] == 4


def test_rerank_off_sends_the_pre_rerank_request_shape() -> None:
    """The raw path stays reachable, and pinned, as an EXPLICIT ``rerank=False``.

    It is the shape the committed 2026-08-16 baseline was measured on, so it has
    to keep existing byte-identical even though it is no longer the default —
    the eval harness scores raw and reranked side by side, and a bursty caller
    needs it because Cohere Rerank 3.5 is 3 req/min account-wide (not
    adjustable). Re-aimed, not deleted, when the default flipped.
    """
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", top_k=4, rerank=False)

    search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert search == {"numberOfResults": 4}


def test_rerank_pool_never_narrows_below_top_k() -> None:
    """A pool below k would make the rerank step DROP results the caller asked for."""
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve(
        "q", top_k=10, rerank=True, rerank_pool=3
    )

    search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert search["numberOfResults"] == 10


def test_rerank_composes_with_the_evidence_floor() -> None:
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve(
        "q", top_k=5, min_evidence=1, rerank=True
    )

    search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert "filter" in search
    assert "rerankingConfiguration" in search


def test_search_type_hybrid_lands_in_the_request() -> None:
    """``overrideSearchType`` rides inside ``vectorSearchConfiguration``.

    The default (``None``) is pinned by
    ``test_rerank_off_sends_the_pre_rerank_request_shape``'s exact-equality
    assert — no key unless asked — so this only has to prove the opt-in.
    """
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve(
        "q", rerank=False, search_type="HYBRID"
    )

    search = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert search["overrideSearchType"] == "HYBRID"


def test_search_type_typo_fails_before_any_request() -> None:
    """A bad value raises locally instead of burning an AWS round-trip."""
    fake = FakeAgentRuntime([])
    with pytest.raises(ValueError, match="search_type"):
        KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", search_type="hybrid")
    assert fake.calls == []


# ── The RDS inverted-score correction (measured 2026-08-20) ────────────────


def _worst_first() -> list[dict[str, Any]]:
    """What an Aurora/RDS KB actually returns: score = cosine distance,
    ordered descending — i.e. the least similar chunk first."""
    return [
        kb_result("worst", uri="s3://b/corpus/worst.md", score=0.83),
        kb_result("middle", uri="s3://b/corpus/middle.md", score=0.80),
        kb_result("best", uri="s3://b/corpus/best.md", score=0.77),
    ]


def test_semantic_score_is_distance_reorders_ascending() -> None:
    fake = FakeAgentRuntime(_worst_first())
    client = KnowledgeBaseClient(
        "kb-123", agent_runtime_client=fake, semantic_score_is_distance=True
    )
    passages = client.retrieve("q", rerank=False)
    assert [p.text for p in passages] == ["best", "middle", "worst"]


def test_the_correction_leaves_hybrid_alone() -> None:
    """HYBRID scores are fusion ranks, higher-is-better on both stores —
    re-sorting them ascending would invert a correct list."""
    fake = FakeAgentRuntime(_worst_first())
    client = KnowledgeBaseClient(
        "kb-123", agent_runtime_client=fake, semantic_score_is_distance=True
    )
    passages = client.retrieve("q", rerank=False, search_type="HYBRID")
    assert [p.text for p in passages] == ["worst", "middle", "best"]


def test_the_correction_leaves_reranked_results_alone() -> None:
    fake = FakeAgentRuntime(_worst_first())
    client = KnowledgeBaseClient(
        "kb-123", agent_runtime_client=fake, semantic_score_is_distance=True
    )
    passages = client.retrieve("q", rerank=True)
    assert [p.text for p in passages] == ["worst", "middle", "best"]


def test_the_default_never_reorders() -> None:
    """S3 Vectors orders correctly; a silent re-sort there would be a new bug."""
    fake = FakeAgentRuntime(_worst_first())
    passages = KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", rerank=False)
    assert [p.text for p in passages] == ["worst", "middle", "best"]
