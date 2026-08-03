from docpipe_core.models import Job, JobStatus, SummaryResult


def test_job_defaults() -> None:
    job = Job(document_key="documents/abc")
    assert job.status is JobStatus.PENDING
    assert len(job.job_id) == 32
    assert job.result is None
    assert job.created_at.tzinfo is not None


def test_job_json_round_trip() -> None:
    job = Job(
        document_key="documents/abc",
        status=JobStatus.COMPLETED,
        result=SummaryResult(
            summary="short",
            model_id="us.deepseek.r1-v1:0",
            input_tokens=100,
            output_tokens=20,
            latency_ms=1500,
        ),
    )
    restored = Job.model_validate(job.model_dump(mode="json"))
    assert restored == job
