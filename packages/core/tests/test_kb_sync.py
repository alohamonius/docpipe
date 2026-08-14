import json
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


def sidecar_for(doc: Path, max_evidence: int = 3) -> Path:
    """Write the `<doc>.metadata.json` companion Bedrock reads alongside `doc`."""
    path = doc.with_name(doc.name + ".metadata.json")
    attributes = {
        "metadataAttributes": {
            "maxEvidence": {
                "value": {"type": "NUMBER", "numberValue": max_evidence},
                "includeForEmbedding": False,
            }
        }
    }
    path.write_text(json.dumps(attributes))
    return path


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


# ── Metadata sidecars ──────────────────────────────────────────────────────
# Bedrock reads `<file>.metadata.json` next to `<file>` and attaches its
# attributes to every vector from that file. Dropping them silently costs
# retrieval-time evidence filtering and named citations, so they are pinned.


def test_sidecar_uploads_with_its_document(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    sidecar_for(tmp_path / "index.md")

    report = syncer(s3_bucket, FakeAgent(["STARTING"])).sync(tmp_path, start_ingestion=False)

    assert [d.key for d in report.sidecars] == ["corpus/index.md.metadata.json"]
    # Documents stay countable on their own — a sidecar is not a document.
    assert [d.key for d in report.documents] == ["corpus/anatomy/hamstrings.md", "corpus/index.md"]
    stored = s3_bucket.get_object(Bucket=BUCKET, Key="corpus/index.md.metadata.json")
    assert json.loads(stored["Body"].read())["metadataAttributes"]["maxEvidence"]
    assert stored["ContentType"] == "application/json"


def test_orphan_sidecar_is_ignored(tmp_path: Path) -> None:
    make_corpus(tmp_path)
    # No `ghost.md` — this sidecar describes a document that does not exist.
    (tmp_path / "ghost.md.metadata.json").write_text("{}")

    assert [p.name for p in discover_corpus(tmp_path)] == ["hamstrings.md", "index.md"]


def test_sidecar_only_edit_still_reuploads(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    sidecar = sidecar_for(tmp_path / "index.md", max_evidence=3)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    # Regrade the chunk without touching a byte of its prose. If change
    # detection only watched the .md, this edit would never reach Bedrock and
    # an evidence filter would silently act on the old grade.
    sidecar_for(tmp_path / "index.md", max_evidence=1)
    second = syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    assert [d.key for d in second.uploaded] == ["corpus/index.md.metadata.json"]
    assert [d.key for d in second.skipped] == ["corpus/anatomy/hamstrings.md", "corpus/index.md"]
    # The new grade really replaced the old one in S3.
    stored = s3_bucket.get_object(Bucket=BUCKET, Key="corpus/index.md.metadata.json")
    assert stored["Body"].read().decode() == sidecar.read_text()


# ── Pruning: the bucket equals the last build, deletions included ───────────
# Chunk keys are slugified section headings, so renaming a heading in `docs/`
# mints a new key and — before this — left the old one in the bucket, embedded
# and retrievable forever. Two chunks then answer the same question and the
# stale one is indistinguishable at retrieval time. See
# how2doo `goals/kb-retrieval-readiness/02-sync-fidelity/goal.md`.


def remote_keys(s3_client: Any, prefix: str = "") -> list[str]:
    """Every key currently in the test bucket, sorted."""
    got = s3_client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return sorted(obj["Key"] for obj in got.get("Contents", []))


def make_wide_corpus(root: Path, docs: int = 20) -> list[Path]:
    """``docs`` chunks, each with a sidecar — wide enough that ONE rename is a
    small fraction of it, the way it is in the real 383-chunk build. A 2-file
    corpus would make every rename a 50% deletion and never exercise the
    default blast-radius ratio.
    """
    (root / "anatomy").mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for i in range(docs):
        doc = root / "anatomy" / f"{i:02d}-myofascial-chains--01-overview.md"
        doc.write_text(f"# Chain {i}\n\nEvidence: ★★☆\n")
        sidecar_for(doc)
        written.append(doc)
    return written


def drop(doc: Path) -> None:
    """Remove a chunk the way a rebuild does — document and sidecar together."""
    doc.with_name(doc.name + ".metadata.json").unlink()
    doc.unlink()


class RecordingS3:
    """Moto client that records the exact order of delete_object calls."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.deleted: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def delete_object(self, **kwargs: Any) -> Any:
        self.deleted.append(kwargs["Key"])
        return self._inner.delete_object(**kwargs)


def test_renamed_chunk_stops_being_retrievable(tmp_path: Path, s3_bucket) -> None:
    """The headline case. A heading rewrite renames the key; the old one must go."""
    docs = make_wide_corpus(tmp_path)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    assert len(remote_keys(s3_bucket)) == 40  # 20 documents + 20 sidecars

    # `## Overview` → `## What this chain is` in docs/ ⇒ a new slug, same content.
    old = docs[0]
    new = old.with_name("00-myofascial-chains--01-what-this-chain-is.md")
    old.with_name(old.name + ".metadata.json").rename(new.with_name(new.name + ".metadata.json"))
    old.rename(new)

    report = syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    live = remote_keys(s3_bucket)
    assert "corpus/anatomy/00-myofascial-chains--01-overview.md" not in live
    assert "corpus/anatomy/00-myofascial-chains--01-overview.md.metadata.json" not in live
    assert "corpus/anatomy/00-myofascial-chains--01-what-this-chain-is.md" in live
    assert len(live) == 40  # the rename replaced, it did not accumulate
    assert [p.key for p in report.pruned] == [
        "corpus/anatomy/00-myofascial-chains--01-overview.md",
        "corpus/anatomy/00-myofascial-chains--01-overview.md.metadata.json",
    ]


def test_prune_deletes_the_document_before_its_sidecar(tmp_path: Path, s3_bucket) -> None:
    """Order is a safety property, not a detail.

    A `.md` left without its `.metadata.json` is a *silent* defect: Bedrock
    re-embeds it with no attributes, so an unrated chunk sails through a
    `min_evidence` filter. The reverse — a sidecar with no document — is inert.
    A prune that dies half-way must die on the inert side.
    """
    docs = make_wide_corpus(tmp_path)
    recorder = RecordingS3(s3_bucket)
    syncer(recorder, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    drop(docs[0])

    syncer(recorder, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    assert recorder.deleted == [
        "corpus/anatomy/00-myofascial-chains--01-overview.md",
        "corpus/anatomy/00-myofascial-chains--01-overview.md.metadata.json",
    ]


def test_prune_leaves_keys_outside_the_prefix_alone(tmp_path: Path, s3_bucket) -> None:
    make_wide_corpus(tmp_path)
    s3_bucket.put_object(Bucket=BUCKET, Key="backups/whatever.md", Body=b"not ours")

    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    assert "backups/whatever.md" in remote_keys(s3_bucket)


def test_prune_can_be_switched_off(tmp_path: Path, s3_bucket) -> None:
    """`prune=False` is the pre-fix behaviour, kept reachable and named."""
    docs = make_wide_corpus(tmp_path)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    drop(docs[0])

    report = syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False, prune=False)

    assert report.pruned == []
    assert "corpus/anatomy/00-myofascial-chains--01-overview.md" in remote_keys(s3_bucket)


def test_sync_with_nothing_to_prune_issues_no_deletes(tmp_path: Path, s3_bucket) -> None:
    make_wide_corpus(tmp_path)
    recorder = RecordingS3(s3_bucket)
    syncer(recorder, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    report = syncer(recorder, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    assert recorder.deleted == []
    assert report.pruned == []


def test_plan_prune_reports_orphans_without_touching_the_bucket(tmp_path: Path, s3_bucket) -> None:
    """The audit path: what a prune *would* remove, with nothing removed."""
    docs = make_wide_corpus(tmp_path)
    recorder = RecordingS3(s3_bucket)
    syncer(recorder, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    drop(docs[1])

    orphans = syncer(recorder, FakeAgent([])).plan_prune(tmp_path)

    assert [p.key for p in orphans] == [
        "corpus/anatomy/01-myofascial-chains--01-overview.md",
        "corpus/anatomy/01-myofascial-chains--01-overview.md.metadata.json",
    ]
    assert [p.sidecar for p in orphans] == [False, True]
    assert recorder.deleted == []
    assert len(remote_keys(s3_bucket)) == 40


# ── The blast-radius guard ─────────────────────────────────────────────────
# Remote-minus-local is only as safe as the local tree is trustworthy. Point it
# at an unbuilt or half-built `build/kb` and the same diff wipes the corpus.


def test_guard_refuses_to_wipe_the_bucket_for_an_empty_local_tree(
    tmp_path: Path, s3_bucket
) -> None:
    from docpipe_core.kb_sync import BlastRadiusRefused

    make_wide_corpus(tmp_path / "built")
    recorder = RecordingS3(s3_bucket)
    syncer(recorder, FakeAgent([])).sync(tmp_path / "built", start_ingestion=False)

    empty = tmp_path / "unbuilt"
    empty.mkdir()
    with pytest.raises(BlastRadiusRefused) as exc:
        syncer(recorder, FakeAgent([])).sync(empty, start_ingestion=False)

    assert recorder.deleted == []
    assert len(remote_keys(s3_bucket)) == 40
    assert "40 of 40" in str(exc.value)
    assert exc.value.ratio == 1.0


def test_guard_refuses_a_half_built_corpus_and_names_the_numbers(tmp_path: Path, s3_bucket) -> None:
    from docpipe_core.kb_sync import BlastRadiusRefused

    docs = make_wide_corpus(tmp_path)
    recorder = RecordingS3(s3_bucket)
    syncer(recorder, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    for doc in docs[:15]:  # the build crashed after 5 of 20 chunks
        drop(doc)

    with pytest.raises(BlastRadiusRefused) as exc:
        syncer(recorder, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    assert exc.value.ratio == pytest.approx(0.75)
    assert exc.value.limit == pytest.approx(0.10)
    assert "30 of 40" in str(exc.value)
    assert "--max-delete-ratio" in str(exc.value)  # the escape hatch is in the message
    assert recorder.deleted == []


def test_guard_refuses_before_uploading_anything(tmp_path: Path, s3_bucket) -> None:
    """A refusal means 'this tree is not trustworthy' — so nothing lands either."""
    from docpipe_core.kb_sync import BlastRadiusRefused

    docs = make_wide_corpus(tmp_path)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    for doc in docs[:15]:
        drop(doc)
    docs[19].write_text("# Chain 19\n\nEdited, and this edit must NOT land.\n")

    with pytest.raises(BlastRadiusRefused):
        syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    body = s3_bucket.get_object(
        Bucket=BUCKET, Key="corpus/anatomy/19-myofascial-chains--01-overview.md"
    )["Body"].read()
    assert b"must NOT land" not in body


def test_guard_never_starts_an_ingestion_job(tmp_path: Path, s3_bucket) -> None:
    from docpipe_core.kb_sync import BlastRadiusRefused

    docs = make_wide_corpus(tmp_path)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    for doc in docs[:15]:
        drop(doc)

    agent = FakeAgent(["STARTING", "COMPLETE"])
    with pytest.raises(BlastRadiusRefused):
        syncer(s3_bucket, agent).sync(tmp_path, wait=False)

    assert agent.start_calls == []


def test_guard_is_a_ratio_not_a_veto(tmp_path: Path, s3_bucket) -> None:
    """A deliberate reshape (05-chunk-shape) says the number out loud and proceeds."""
    docs = make_wide_corpus(tmp_path)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    for doc in docs[:15]:
        drop(doc)

    report = syncer(s3_bucket, FakeAgent([])).sync(
        tmp_path, start_ingestion=False, max_delete_ratio=0.8
    )

    assert len(report.pruned) == 30
    assert len(remote_keys(s3_bucket)) == 10


def test_default_ratio_admits_the_biggest_routine_rename(tmp_path: Path, s3_bucket) -> None:
    """10% is not arbitrary — measured against the 2026-08-14 corpus.

    383 documents + 383 sidecars. The largest family a routine edit retires is
    `body-graph-structure` at 31 chunks = 8.1%; every prose source-doc family is
    ≤ 15 chunks = 3.9%. The 132-chunk `body-graph-connection` family is 34% and
    is *meant* to need the flag: retiring a third of the corpus wants a human.
    """
    from docpipe_core.kb_sync import DEFAULT_MAX_DELETE_RATIO

    assert 31 / 383 < DEFAULT_MAX_DELETE_RATIO < 132 / 383

    docs = make_wide_corpus(tmp_path)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)
    drop(docs[0])  # 2 of 40 keys = 5%, under the default

    report = syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    assert len(report.pruned) == 2
