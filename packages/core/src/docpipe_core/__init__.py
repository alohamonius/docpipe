"""docpipe-core: shared library for health.studio's AWS AI services.

- models: Pydantic domain models (Job, JobStatus, SummaryResult, ChatMessage, ChatReply)
- storage: S3 documents + DynamoDB job records
- conversations: DynamoDB conversation history (PK userId, SK conversationId, TTL)
- queue: SQS publish/consume
- llm: Bedrock via the Converse API (DeepSeek default) — ChatClient + SummarizerClient
- agent: the Converse tool loop — KbAgent drives search_kb over a Knowledge Base
- chat: ChatService — one POST /chat turn: history → KbAgent → persist → reply + citations
- observability: structured logging + CloudWatch EMF metrics
"""

from docpipe_core.agent import AgentSession, KbAgent, ToolCall
from docpipe_core.chat import ChatRequest, ChatResponse, ChatService, Citation
from docpipe_core.conversations import ConversationStore
from docpipe_core.graph import BodyGraph, GraphEdge, GraphHop, GraphNode, build_graph, expand
from docpipe_core.kb_sync import (
    DEFAULT_MAX_DELETE_RATIO,
    DEFAULT_MAX_DOC_BYTES,
    BlastRadiusRefused,
    CorpusSyncer,
    IngestionOutcome,
    OversizedDocRefused,
    PlannedDoc,
    PrunedKey,
    SyncAction,
    SyncReport,
    discover_corpus,
)
from docpipe_core.llm import (
    HEALTH_ASSISTANT_SYSTEM,
    ChatClient,
    ModelInvocationError,
    SummarizerClient,
)
from docpipe_core.models import (
    ChatMessage,
    ChatReply,
    Conversation,
    Job,
    JobStatus,
    SummaryResult,
)
from docpipe_core.observability import configure_logging, emit_metric, get_logger
from docpipe_core.queue import JobQueue, QueueMessage
from docpipe_core.retrieval import KnowledgeBaseClient, RetrievedPassage
from docpipe_core.storage import DocumentStore, JobStore

__version__ = "0.1.0"

__all__ = [
    "BodyGraph",
    "GraphEdge",
    "GraphHop",
    "GraphNode",
    "build_graph",
    "expand",
    "DEFAULT_MAX_DELETE_RATIO",
    "DEFAULT_MAX_DOC_BYTES",
    "HEALTH_ASSISTANT_SYSTEM",
    "AgentSession",
    "BlastRadiusRefused",
    "ChatClient",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ChatService",
    "Citation",
    "ChatReply",
    "Conversation",
    "ConversationStore",
    "CorpusSyncer",
    "DocumentStore",
    "IngestionOutcome",
    "Job",
    "JobQueue",
    "JobStatus",
    "JobStore",
    "KbAgent",
    "KnowledgeBaseClient",
    "ModelInvocationError",
    "OversizedDocRefused",
    "PlannedDoc",
    "PrunedKey",
    "QueueMessage",
    "RetrievedPassage",
    "SummarizerClient",
    "SummaryResult",
    "SyncAction",
    "SyncReport",
    "ToolCall",
    "configure_logging",
    "discover_corpus",
    "emit_metric",
    "get_logger",
]
