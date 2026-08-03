from typing import Any

from docpipe_core.llm import HEALTH_ASSISTANT_SYSTEM, ChatClient
from docpipe_core.models import ChatMessage
from docpipe_core.retrieval import KnowledgeBaseClient, RetrievedPassage
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
