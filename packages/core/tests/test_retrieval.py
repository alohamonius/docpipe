from typing import Any

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
    assert call["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 2


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


def test_rerank_off_sends_the_pre_rerank_request_shape() -> None:
    """The default path must stay byte-identical to before reranking existed —
    it is the shape the committed 2026-08-16 baseline was measured on."""
    fake = FakeAgentRuntime([])
    KnowledgeBaseClient("kb-123", agent_runtime_client=fake).retrieve("q", top_k=4)

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
