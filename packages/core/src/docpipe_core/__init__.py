"""docpipe-core: shared library for docpipe services.

Modules (built in Phase 1, see PLAN.md):
- models: Pydantic domain models (Job, JobStatus, SummaryRequest, SummaryResult)
- storage: S3 documents + DynamoDB job records
- queue: SQS publish/consume
- llm: Bedrock (Claude) invocation with retries and token accounting
- observability: structured logging + CloudWatch EMF metrics
"""

__version__ = "0.1.0"
