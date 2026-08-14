"""docpipe-core: shared library for health.studio's AWS AI services.

- models: Pydantic domain models (Job, JobStatus, SummaryResult, ChatMessage, ChatReply)
- storage: S3 documents + DynamoDB job records
- queue: SQS publish/consume
- llm: Bedrock via the Converse API (DeepSeek default) — ChatClient + SummarizerClient
- observability: structured logging + CloudWatch EMF metrics
"""

from docpipe_core.kb_sync import (
    DEFAULT_MAX_DELETE_RATIO,
    BlastRadiusRefused,
    CorpusSyncer,
    IngestionOutcome,
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
    "DEFAULT_MAX_DELETE_RATIO",
    "HEALTH_ASSISTANT_SYSTEM",
    "BlastRadiusRefused",
    "ChatClient",
    "ChatMessage",
    "ChatReply",
    "Conversation",
    "CorpusSyncer",
    "DocumentStore",
    "IngestionOutcome",
    "Job",
    "JobQueue",
    "JobStatus",
    "JobStore",
    "KnowledgeBaseClient",
    "ModelInvocationError",
    "PlannedDoc",
    "PrunedKey",
    "QueueMessage",
    "RetrievedPassage",
    "SummarizerClient",
    "SummaryResult",
    "SyncAction",
    "SyncReport",
    "configure_logging",
    "discover_corpus",
    "emit_metric",
    "get_logger",
]
