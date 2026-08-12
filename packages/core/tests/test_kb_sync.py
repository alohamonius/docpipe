from pathlib import Path
from typing import Any

import pytest
from docpipe_core.kb_sync import CorpusSyncer, SyncAction, discover_corpus

BUCKET = "docpipe-test"  # created by the s3_bucket fixture (conftest)


class FakeAgent:
    """Stand-in for the bedrock-agent control-plane client.

    ``statuses[0]`` is what start_ingestion_job reports; the rest are returned
    by successive get_ingestion_job calls.
    """

    def __init__(self, statuses: list[str], statistics: dict[str, Any] | None = None) -> None:
        self.statuses = list(statuses)
        self.statistics = statistics or {}
        self.start_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def start_ingestion_job(self, **kwargs: Any) -> dict[str, Any]:
        self.start_calls.append(kwargs)
        return {"ingestionJob": {"ingestionJobId": "ing-1", "status": self.statuses.pop(0)}}

    def get_ingestion_job(self, **kwargs: Any) -> dict[str, Any]:
        self.get_calls.append(kwargs)
        status = self.statuses.pop(0)
        job: dict[str, Any] = {"ingestionJobId": "ing-1", "status": status}
        if status in {"COMPLETE", "FAILED", "STOPPED"}:
            job["statistics"] = self.statistics
        return {"ingestionJob": job}


def counting_clock():
    """Deterministic monotonic clock: 0.0, 1.0, 2.0, … per call."""
    state = {"v": -1.0}

    def _clock() -> float:
        state["v"] += 1.0
        return state["v"]

    return _clock


def make_corpus(root: Path) -> None:
    (root / "anatomy").mkdir(parents=True)
    (root / "anatomy" / "hamstrings.md").write_text("Hamstrings cross two joints.\n")
    (root / "index.md").write_text("# Corpus\n")
    (root / "notes.txt").write_text("ignored, not markdown\n")  # non-.md is skipped


def syncer(s3_client: Any, agent: Any) -> CorpusSyncer:
    return CorpusSyncer(
        BUCKET,
        knowledge_base_id="kb-1",
        data_source_id="ds-1",
        s3_client=s3_client,
        agent_client=agent,
    )


def test_discover_corpus_is_sorted_and_markdown_only(tmp_path: Path) -> None:
    make_corpus(tmp_path)
    found = discover_corpus(tmp_path)
    assert [p.name for p in found] == ["hamstrings.md", "index.md"]


def test_plan_maps_relative_paths_to_prefixed_keys(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    planned = syncer(s3_bucket, FakeAgent(["STARTING"])).plan(tmp_path)

    keys = {d.key for d in planned}
    assert keys == {"corpus/anatomy/hamstrings.md", "corpus/index.md"}
    assert all(d.action is SyncAction.UPLOADED for d in planned)  # nothing in S3 yet


def test_sync_uploads_then_skips_unchanged_and_reuploads_edits(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    sub = syncer(s3_bucket, FakeAgent(["STARTING", "COMPLETE"]))

    first = sub.sync(tmp_path, wait=True, sleep=lambda _: None, clock=counting_clock())
    assert {d.action for d in first.uploaded} == {SyncAction.UPLOADED}
    assert len(first.uploaded) == 2
    # Bytes really landed in S3.
    body = s3_bucket.get_object(Bucket=BUCKET, Key="corpus/index.md")["Body"].read()
    assert body == b"# Corpus\n"

    # Second run, nothing changed → everything skipped.
    second = syncer(s3_bucket, FakeAgent(["STARTING", "COMPLETE"])).sync(
        tmp_path, wait=True, sleep=lambda _: None, clock=counting_clock()
    )
    assert len(second.skipped) == 2
    assert second.uploaded == []

    # Edit one file → only that one re-uploads.
    (tmp_path / "index.md").write_text("# Corpus v2\n")
    third = syncer(s3_bucket, FakeAgent(["STARTING", "COMPLETE"])).sync(
        tmp_path, wait=True, sleep=lambda _: None, clock=counting_clock()
    )
    assert [d.key for d in third.uploaded] == ["corpus/index.md"]
    assert [d.key for d in third.skipped] == ["corpus/anatomy/hamstrings.md"]


def test_sync_starts_and_polls_until_complete(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    agent = FakeAgent(
        ["STARTING", "IN_PROGRESS", "COMPLETE"],
        statistics={
            "numberOfDocumentsScanned": 2,
            "numberOfNewDocumentsIndexed": 2,
            "numberOfDocumentsFailed": 0,
        },
    )
    report = syncer(s3_bucket, agent).sync(
        tmp_path, wait=True, timeout=1000.0, sleep=lambda _: None, clock=counting_clock()
    )

    assert agent.start_calls[0] == {"knowledgeBaseId": "kb-1", "dataSourceId": "ds-1"}
    assert len(agent.get_calls) == 2  # IN_PROGRESS, then COMPLETE
    assert agent.get_calls[0]["ingestionJobId"] == "ing-1"
    assert report.ingestion is not None
    assert report.ingestion.succeeded
    assert report.ingestion.documents_indexed == 2
    assert report.ingestion.documents_failed == 0


def test_sync_no_wait_returns_started_status_without_polling(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    agent = FakeAgent(["STARTING"])
    report = syncer(s3_bucket, agent).sync(tmp_path, wait=False)

    assert agent.get_calls == []
    assert report.ingestion is not None
    assert report.ingestion.status == "STARTING"
    assert report.ingestion.documents_indexed is None


def test_sync_times_out_when_job_never_finishes(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    agent = FakeAgent(["STARTING"])  # never polled — times out first
    report = syncer(s3_bucket, agent).sync(
        tmp_path, wait=True, timeout=0.0, sleep=lambda _: None, clock=counting_clock()
    )

    assert agent.get_calls == []
    assert report.ingestion is not None
    assert report.ingestion.status == "TIMEOUT"


def test_start_ingestion_false_uploads_but_never_touches_agent(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    agent = FakeAgent([])
    report = syncer(s3_bucket, agent).sync(tmp_path, start_ingestion=False)

    assert agent.start_calls == []
    assert report.ingestion is None
    assert len(report.uploaded) == 2


def test_sync_rejects_missing_root(tmp_path: Path, s3_bucket) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        syncer(s3_bucket, FakeAgent([])).sync(tmp_path / "nope")
