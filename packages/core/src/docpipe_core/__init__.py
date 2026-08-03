"""docpipe-core: shared library for docpipe services.

- models: Pydantic domain models (Job, JobStatus, SummaryResult)
- storage: S3 documents + DynamoDB job records
- queue: SQS publish/consume
- llm: Bedrock (DeepSeek via Converse API) with retries and token accounting
- observability: structured logging + CloudWatch EMF metrics
"""

from docpipe_core.llm import SummarizationError, SummarizerClient
from docpipe_core.models import Job, JobStatus, SummaryResult
from docpipe_core.observability import configure_logging, emit_metric, get_logger
from docpipe_core.queue import JobQueue, QueueMessage
from docpipe_core.storage import DocumentStore, JobStore

__version__ = "0.1.0"

__all__ = [
    "DocumentStore",
    "Job",
    "JobQueue",
    "JobStatus",
    "JobStore",
    "QueueMessage",
    "SummarizationError",
    "SummarizerClient",
    "SummaryResult",
    "configure_logging",
    "emit_metric",
    "get_logger",
]
