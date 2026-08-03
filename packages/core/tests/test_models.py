from docpipe_core.models import ChatMessage, Conversation, Job, JobStatus, SummaryResult


def test_job_defaults() -> None:
    job = Job(user_id="user1", document_key="documents/abc")
    assert job.status is JobStatus.PENDING
    assert len(job.job_id) == 32
    assert job.result is None
    assert job.created_at.tzinfo is not None


def test_conversation_round_trip() -> None:
    conversation = Conversation(
        user_id="user1",
        messages=[ChatMessage(role="user", content="hi")],
    )
    restored = Conversation.model_validate(conversation.model_dump(mode="json"))
    assert restored == conversation


def test_job_json_round_trip() -> None:
    job = Job(
        document_key="documents/abc",
        user_id="user1",
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
