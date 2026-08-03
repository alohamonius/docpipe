"""Domain models shared by the API service and the worker."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_job_id() -> str:
    return uuid.uuid4().hex


class SummaryResult(BaseModel):
    summary: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatReply(BaseModel):
    message: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class Job(BaseModel):
    job_id: str = Field(default_factory=new_job_id)
    status: JobStatus = JobStatus.PENDING
    document_key: str
    content_type: str = "text/plain"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    result: SummaryResult | None = None
    error: str | None = None
