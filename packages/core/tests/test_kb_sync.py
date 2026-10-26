import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
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


# ── The oversized-document guard ───────────────────────────────────────────
# Titan v2 caps embedding input at 8,192 tokens; with `chunkingStrategy: NONE`
# one file is one embedding call, so an oversized file is a FAILED document at
# ingestion — discovered by polling, minutes after the upload that doomed it.
# The plan refuses first, before anything lands in the bucket.


def oversized_doc(root: Path, words: int = 10_000) -> Path:
    """A document well past the 32 KiB default (≈50 KB ≈ 12.5k estimated tokens)."""
    doc = root / "anatomy" / "encyclopedia.md"
    doc.write_text("word " * words)
    return doc


def test_plan_refuses_a_document_past_the_embedding_cap(tmp_path: Path, s3_bucket) -> None:
    from docpipe_core.kb_sync import OversizedDocRefused

    make_corpus(tmp_path)
    oversized_doc(tmp_path)

    with pytest.raises(OversizedDocRefused) as exc:
        syncer(s3_bucket, FakeAgent([])).plan(tmp_path)

    assert "encyclopedia.md" in str(exc.value)
    assert "8,192" in str(exc.value)  # the cap being proxied is named
    assert "--max-doc-bytes" in str(exc.value)  # and so is the escape hatch


def test_oversized_guard_refuses_before_uploading_anything(tmp_path: Path, s3_bucket) -> None:
    """The small, valid files must not land either — a refused sync mutates nothing."""
    from docpipe_core.kb_sync import OversizedDocRefused

    make_corpus(tmp_path)
    oversized_doc(tmp_path)
    agent = FakeAgent(["STARTING", "COMPLETE"])

    with pytest.raises(OversizedDocRefused):
        syncer(s3_bucket, agent).sync(tmp_path, wait=False)

    assert remote_keys(s3_bucket) == []
    assert agent.start_calls == []


def test_oversized_guard_is_a_limit_not_a_veto(tmp_path: Path, s3_bucket) -> None:
    """A corpus measured to embed anyway says the number out loud and proceeds."""
    make_corpus(tmp_path)
    oversized_doc(tmp_path)

    report = syncer(s3_bucket, FakeAgent([])).sync(
        tmp_path, start_ingestion=False, max_doc_bytes=64 * 1024
    )

    assert "corpus/anatomy/encyclopedia.md" in [d.key for d in report.uploaded]


def test_sidecars_are_exempt_from_the_size_guard(tmp_path: Path, s3_bucket) -> None:
    """A sidecar produces no vector, so the token cap does not apply to it; its
    2 KB filterable-metadata limit is Bedrock's own, enforced at ingestion."""
    make_corpus(tmp_path)
    doc = tmp_path / "index.md"
    doc.with_name(doc.name + ".metadata.json").write_text(
        json.dumps({"metadataAttributes": {}}) + " " * 40_000
    )

    planned = syncer(s3_bucket, FakeAgent([])).plan(tmp_path)

    assert [d.key for d in planned if d.sidecar] == ["corpus/index.md.metadata.json"]


def test_default_limit_admits_the_largest_real_chunk() -> None:
    """32 KiB is measured, not picked.

    UPDATED 2026-08-21. This used to read "the corpus's largest chunk is 24.7 KB
    … ~30% headroom". The corpus has since grown to 585 chunks and the largest
    is now `body-graph-joint--radiocarpal` at 31,072 B — headroom is ~5%, not
    ~30%. The limit still holds, but it is no longer comfortable, which is
    exactly why the build now warns at 90% of the cap.
    """
    from docpipe_core.kb_sync import DEFAULT_MAX_DOC_BYTES, TITAN_V2_MAX_TOKENS

    assert DEFAULT_MAX_DOC_BYTES == TITAN_V2_MAX_TOKENS * 4
    assert DEFAULT_MAX_DOC_BYTES > 31_072


# ── Planning cost: O(1) requests, not O(corpus) ─────────────────────────────
# `plan` used to ask S3 for each file's ETag with its own `head_object`. That
# is the textbook N+1: at 585 chunks plus sidecars it was 1,170 sequential
# round trips and a measured ~8 minutes, for information ListObjectsV2 hands
# over in full for free. The bug is invisible in a 2-file test fixture and
# only bites at real corpus size, so the guard has to be a cost assertion.


def count_s3_calls(client: Any) -> Counter[str]:
    """Live count of S3 API operations by name, keyed off botocore's own event
    bus — so it sees calls made *inside* a paginator, which wrapping the client
    object would miss."""
    counts: Counter[str] = Counter()
    client.meta.events.register("before-call.s3", lambda model, **_: counts.update([model.name]))
    return counts


def test_planning_costs_the_same_whether_the_corpus_is_4_files_or_40(
    tmp_path: Path, s3_bucket
) -> None:
    """The N+1 regression test. Ten times the files, byte-identical request
    count — that equality is only possible if nothing is asked per file."""
    small, large = tmp_path / "small", tmp_path / "large"
    make_wide_corpus(small, docs=4)
    make_wide_corpus(large, docs=40)

    counts = count_s3_calls(s3_bucket)
    syncer(s3_bucket, FakeAgent([])).plan(small)
    small_calls = sum(counts.values())
    counts.clear()
    syncer(s3_bucket, FakeAgent([])).plan(large)

    assert sum(counts.values()) == small_calls
    assert counts["HeadObject"] == 0


def test_planning_a_synced_corpus_reads_etags_from_one_listing(tmp_path: Path, s3_bucket) -> None:
    """The populated case: 40 documents already in the bucket, every one of
    them correctly recognised as unchanged, and still not one HeadObject."""
    make_wide_corpus(tmp_path, docs=40)
    syncer(s3_bucket, FakeAgent([])).sync(tmp_path, start_ingestion=False)

    counts = count_s3_calls(s3_bucket)
    planned = syncer(s3_bucket, FakeAgent([])).plan(tmp_path)

    assert len(planned) == 80  # 40 documents + 40 sidecars
    assert {d.action for d in planned} == {SyncAction.SKIPPED}
    assert counts["HeadObject"] == 0
    assert counts["ListObjectsV2"] == 1


# ── Which files actually feed the corpus ────────────────────────────────────
# `docs/` is not the corpus. The build allowlists directories and excludes
# named files, and the manifest records both. A staleness check that walks
# `docs/**` by mtime instead flags `docs/FINDINGS.md` — an engineering log,
# edited after nearly every session, that produces zero chunks. See the
# 2026-08-21 FINDINGS entry: this fired on a real sync.

MANIFEST_SHAPE = {
    "allowedDirectories": ["anatomy", "exercise-science"],
    "excludedDocs": [
        {"path": "FINDINGS.md", "reason": "root-level doc — not in an allowlisted directory"},
        {"path": "anatomy/referral-boundary.md", "reason": "KB build policy, not knowledge"},
    ],
}


@pytest.mark.parametrize(
    "path, feeds",
    [
        ("anatomy/12-pain-science.md", True),
        ("exercise-science/01-emg.md", True),
        ("FINDINGS.md", False),  # the one that actually cried wolf
        ("PM.md", False),  # root-level, not even named in excludedDocs
        ("anatomy/referral-boundary.md", False),  # allowlisted dir, excluded by name
    ],
)
def test_only_allowlisted_docs_count_as_corpus_inputs(path: str, feeds: bool) -> None:
    from docpipe_core.kb_sync import feeds_the_corpus

    assert feeds_the_corpus(path, MANIFEST_SHAPE) is feeds


def test_a_manifest_without_the_keys_over_warns_rather_than_going_silent() -> None:
    """Old manifests predate `allowedDirectories`. Over-warning is recoverable;
    silently ingesting a stale corpus is not."""
    from docpipe_core.kb_sync import feeds_the_corpus

    assert feeds_the_corpus("FINDINGS.md", {}) is True


# ── Ingestion statistics: new AND modified ──────────────────────────────────


def test_indexed_counts_modified_documents_too(tmp_path: Path, s3_bucket) -> None:
    """Bedrock splits the count in two. Reading only `New` understates every
    job and reports exactly 0 for the commonest case — a sync that edits
    existing chunks and adds none, which is indistinguishable from a no-op.
    Numbers are job FWU5IEPVHS, 2026-08-21: 89 + 7 = the 96 changed documents.
    """
    make_corpus(tmp_path)
    agent = FakeAgent(
        ["STARTING", "COMPLETE"],
        statistics={
            "numberOfDocumentsScanned": 585,
            "numberOfNewDocumentsIndexed": 89,
            "numberOfModifiedDocumentsIndexed": 7,
            "numberOfDocumentsFailed": 0,
        },
    )
    report = syncer(s3_bucket, agent).sync(
        tmp_path, wait=True, sleep=lambda _: None, clock=counting_clock()
    )

    assert report.ingestion is not None
    assert report.ingestion.documents_indexed == 96


def test_an_edit_only_ingest_does_not_report_zero(tmp_path: Path, s3_bucket) -> None:
    """The regression that motivated it: nothing new, seven edits, and the old
    reading printed `indexed=0` while seven documents were re-embedded."""
    make_corpus(tmp_path)
    agent = FakeAgent(
        ["STARTING", "COMPLETE"],
        statistics={"numberOfNewDocumentsIndexed": 0, "numberOfModifiedDocumentsIndexed": 7},
    )
    report = syncer(s3_bucket, agent).sync(
        tmp_path, wait=True, sleep=lambda _: None, clock=counting_clock()
    )

    assert report.ingestion is not None
    assert report.ingestion.documents_indexed == 7


def test_absent_statistics_stay_none_not_zero(tmp_path: Path, s3_bucket) -> None:
    """`None` means Bedrock said nothing; `0` means it indexed nothing. A job
    still STARTING must not claim it indexed zero documents."""
    make_corpus(tmp_path)
    report = syncer(s3_bucket, FakeAgent(["STARTING"])).sync(tmp_path, wait=False)

    assert report.ingestion is not None
    assert report.ingestion.documents_indexed is None


# ── Aurora auto-pause on the INGEST path ────────────────────────────────────
# `min_capacity = 0` means the cluster is asleep by design, and a sync runs
# minutes after a build — long past any keep-alive — so StartIngestionJob is
# very often the call that wakes it. It surfaces as ValidationException, which
# no generic retry configuration treats as transient. Measured 2026-08-22: the
# CLI crashed outright, mid-run, *after* the upload had already happened.


def auto_paused() -> ClientError:
    """The real message, verbatim from the 2026-08-22 crash."""
    return ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": (
                    "The knowledge base storage configuration provided is invalid... "
                    "The vector database encountered an error while processing the "
                    "request: The Aurora DB instance db-L3DM7APXX43HCTKXNZMEBRRQCQ is "
                    "resuming after being auto-paused. Please wait a few seconds and "
                    "try again. (Service: RdsData, Status Code: 400)"
                ),
            }
        },
        "StartIngestionJob",
    )


class ColdAgent(FakeAgent):
    """Refuses the first ``cold`` StartIngestionJob calls the way Aurora does."""

    def __init__(self, statuses: list[str], cold: int = 1, **kwargs: Any) -> None:
        super().__init__(statuses, **kwargs)
        self.cold = cold
        self.refusals = 0

    def start_ingestion_job(self, **kwargs: Any) -> dict[str, Any]:
        if self.refusals < self.cold:
            self.refusals += 1
            raise auto_paused()
        return super().start_ingestion_job(**kwargs)


def test_ingestion_survives_an_aurora_cold_start(tmp_path: Path, s3_bucket) -> None:
    make_corpus(tmp_path)
    agent = ColdAgent(["STARTING", "COMPLETE"], cold=1)

    report = syncer(s3_bucket, agent).sync(
        tmp_path, wait=True, sleep=lambda _: None, clock=counting_clock()
    )

    assert agent.refusals == 1
    assert report.ingestion is not None
    assert report.ingestion.succeeded


def test_a_cluster_that_never_wakes_still_raises(tmp_path: Path, s3_bucket) -> None:
    """Retry, not swallow. Past the budget the operator must see the failure."""
    make_corpus(tmp_path)
    agent = ColdAgent(["STARTING", "COMPLETE"], cold=99)

    with pytest.raises(ClientError, match="auto-paused"):
        CorpusSyncer(
            BUCKET,
            knowledge_base_id="kb-1",
            data_source_id="ds-1",
            s3_client=s3_bucket,
            agent_client=agent,
            max_retries=2,
            rng=lambda: 0.0,
        ).sync(tmp_path, wait=False, sleep=lambda _: None)

    assert agent.refusals == 3  # the initial attempt + 2 retries


def test_a_non_transient_validation_error_is_not_retried(tmp_path: Path, s3_bucket) -> None:
    """The auto-pause arm matches on the resume sentence, not on the error code
    — otherwise a genuinely invalid storage config would be retried four times
    before reporting a misconfiguration that was never going to fix itself."""
    make_corpus(tmp_path)

    class Misconfigured(FakeAgent):
        def __init__(self) -> None:
            super().__init__([])
            self.attempts = 0

        def start_ingestion_job(self, **kwargs: Any) -> dict[str, Any]:
            self.attempts += 1
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "dataSourceId not found"}},
                "StartIngestionJob",
            )

    agent = Misconfigured()
    with pytest.raises(ClientError, match="not found"):
        syncer(s3_bucket, agent).sync(tmp_path, sleep=lambda _: None)

    assert agent.attempts == 1  # failed fast
