"""Knowledge-base retrieval via Bedrock Knowledge Bases.

The KB is built from health.studio's public, evidence-graded content
(anatomy docs, referenced articles). Retrieval returns passages with their
source URIs so chat replies can cite them.
"""

from __future__ import annotations

from typing import Any

import boto3
from pydantic import BaseModel


class RetrievedPassage(BaseModel):
    text: str
    source: str | None = None
    score: float | None = None


class KnowledgeBaseClient:
    def __init__(self, knowledge_base_id: str, agent_runtime_client: Any = None) -> None:
        self.knowledge_base_id = knowledge_base_id
        self._client = agent_runtime_client or boto3.client("bedrock-agent-runtime")

    def retrieve(self, query: str, top_k: int = 4) -> list[RetrievedPassage]:
        response = self._client.retrieve(
            knowledgeBaseId=self.knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
        )
        passages = []
        for result in response.get("retrievalResults", []):
            passages.append(
                RetrievedPassage(
                    text=result.get("content", {}).get("text", ""),
                    source=_source_of(result),
                    score=result.get("score"),
                )
            )
        return passages


def _source_of(result: dict[str, Any]) -> str | None:
    metadata = result.get("metadata", {})
    if uri := metadata.get("x-amz-bedrock-kb-source-uri"):
        return str(uri)
    location = result.get("location", {})
    for key in ("s3Location", "webLocation"):
        if inner := location.get(key):
            return inner.get("uri") or inner.get("url")
    return None
