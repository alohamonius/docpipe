import pytest
from docpipe_core.models import Job, JobStatus, SummaryResult
from docpipe_core.storage import DocumentStore, JobStore


def test_document_put_and_get(s3_bucket) -> None:
    store = DocumentStore("docpipe-test", s3_client=s3_bucket)
    key = store.put_document("job1", b"hello world", "text/plain")
    assert key == "documents/job1"
    assert store.get_document(key) == b"hello world"


def test_job_save_and_get(jobs_table) -> None:
    store = JobStore("docpipe-jobs", dynamodb_resource=jobs_table)
    job = Job(user_id="user1", document_key="documents/abc")
    store.save(job)
    loaded = store.get(job.job_id)
    assert loaded == job


def test_job_get_missing_returns_none(jobs_table) -> None:
    store = JobStore("docpipe-jobs", dynamodb_resource=jobs_table)
    assert store.get("nope") is None


def test_update_status_with_result(jobs_table) -> None:
    store = JobStore("docpipe-jobs", dynamodb_resource=jobs_table)
    job = Job(user_id="user1", document_key="documents/abc")
    store.save(job)

    result = SummaryResult(
        summary="short", model_id="m", input_tokens=10, output_tokens=5, latency_ms=42
    )
    store.update_status(job.job_id, JobStatus.COMPLETED, result=result)

    loaded = store.get(job.job_id)
    assert loaded is not None
    assert loaded.status is JobStatus.COMPLETED
    assert loaded.result == result
    assert loaded.updated_at >= job.updated_at


def test_update_status_missing_job_raises(jobs_table) -> None:
    store = JobStore("docpipe-jobs", dynamodb_resource=jobs_table)
    with pytest.raises(Exception, match="ConditionalCheckFailed"):
        store.update_status("nope", JobStatus.FAILED, error="boom")
