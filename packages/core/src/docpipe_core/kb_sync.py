"""Sync health.studio's public corpus into the Bedrock Knowledge Base.

Pipeline (mirrors the `kb` Pulumi component): local markdown → the KB **source** S3
bucket → `StartIngestionJob` → Bedrock chunks each file and embeds it with
Titan v2 (1024-dim) → the vectors land in the S3 Vectors index that
`KnowledgeBaseClient.retrieve()` later queries at chat time.

Two AWS clients here, deliberately different — easy to conflate:
  * ``s3``            — uploads the markdown files.
  * ``bedrock-agent`` — the *control-plane* client that starts/polls the
    ingestion job. (Retrieval uses ``bedrock-agent-runtime``; ingestion does
    not. Same "bedrock-agent" family name, different endpoint.)

Everything is dependency-injected, so the whole module is unit-tested with
moto (S3) + a fake agent client — no AWS, no spend. Two properties worth
knowing:

  * **Upload is incremental locally.** We skip files whose bytes already match
    the object in S3 (ETag == content MD5 for single-part, SSE-S3 objects), so
    re-running only re-PUTs what changed.
  * **The sync is a mirror, not an append.** Keys under the prefix that the
    local build no longer produces are *deleted* (`prune=True`, the default), so
    the bucket equals the last `pnpm kb:build` output. This is not tidy-up:
    chunk keys are slugified section headings, so renaming a heading in `docs/`
    mints a new key, and before pruning existed the old one stayed in the bucket
    — embedded, retrievable, indistinguishable from the live chunk at query
    time, forever. Ordinary editing was enough to trigger it.
  * **Ingestion is incremental on Bedrock's side.** `StartIngestionJob`
    re-embeds only added/changed/deleted documents, so re-running after editing
    one doc is cheap — you don't pay to re-embed the whole corpus. Bedrock's
    "deleted" means *the object is gone from the bucket*, which is precisely
    what the prune above makes true; without it that word described nothing.
  * **A prune is guarded, because remote-minus-local trusts the local tree.**
    Point a mirror at an unbuilt or half-built `build/kb` and the same diff
    empties the corpus. `max_delete_ratio` (default 10%) refuses first and
    mutates nothing. See `BlastRadiusRefused`.
  * **Metadata sidecars ride along.** A `<file>.md.metadata.json` next to a
    document is uploaded with it, and Bedrock attaches its attributes to every
    vector from that document. That is what makes `min_evidence` filtering and
    named citations possible downstream (see `retrieval.py`). They are pruned
    *with* their document and never before it.
  * **An oversized document is refused at plan time.** Titan v2 caps embedding
    input at 8,192 tokens, and `chunkingStrategy: NONE` makes one file one
    embedding call — so a too-big file is a FAILED document at *ingestion*,
    discovered by polling, minutes after the upload that doomed it. The plan
    refuses first and mutates nothing. See `OversizedDocRefused`.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel

from docpipe_core.observability import get_logger

DEFAULT_PREFIX = "corpus/"
_MARKDOWN_CONTENT_TYPE = "text/markdown"
_JSON_CONTENT_TYPE = "application/json"

# Bedrock's S3 data source attaches `<file>.metadata.json` to every vector it
# derives from `<file>`. health.studio ships one per chunk (docTitle, section,
# maxEvidence, verification, citationCount, safetyCritical, …) — without them
# there is no retrieval-time filtering and a citation is a bare S3 URI.
_SIDECAR_SUFFIX = ".metadata.json"

# Bedrock ingestion-job lifecycle: STARTING → IN_PROGRESS → one of these.
_TERMINAL_STATUSES = frozenset({"COMPLETE", "FAILED", "STOPPED"})
# Set locally when we stop waiting; never returned by Bedrock.
_TIMEOUT_STATUS = "TIMEOUT"

# HeadObject on a missing key surfaces as one of these error codes (HEAD has no
# body, so S3 returns bare "404"; moto/other paths may say NoSuchKey/NotFound).
_MISSING_OBJECT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

# Blast-radius limit: the fraction of the keys under the prefix that one sync may
# delete before it refuses and asks to be told the number explicitly.
#
# 10% is measured, not picked. Against the 2026-08-14 corpus (383 documents +
# 383 sidecars) the largest family a routine edit can retire is the 31
# `body-graph-structure` chunks — 8.1%; every prose source-doc family is ≤ 15
# chunks (3.9%). So every ordinary heading rewrite passes untouched. The
# 132-chunk `body-graph-connection` family is 34% and is *meant* to trip this:
# retiring a third of the corpus is a decision, not a side effect. A half-built
# `build/kb` or a --source one directory too deep lands far above it.
DEFAULT_MAX_DELETE_RATIO = 0.10

# Titan v2 refuses embedding inputs past 8,192 tokens. The guard works in bytes
# because the true tokenizer lives server-side; 4 B/token is deliberately
# generous for this corpus — its largest chunk measures 24.7 KB ≈ 5k tokens
# (~4.9 B/token) — so every real chunk clears the 32 KiB limit with ~30%
# headroom, while a file that trips it is plausibly past the model's cap and
# worth a human look either way. The proxy assumes Latin-script UTF-8:
# CJK/Greek-heavy text runs ~3 B per character at ~1 token per character, so a
# 30 KB file could clear the byte gate yet exceed the token cap — restate this
# before pointing the guard at a non-English corpus.
TITAN_V2_MAX_TOKENS = 8192
_BYTES_PER_TOKEN = 4
DEFAULT_MAX_DOC_BYTES = TITAN_V2_MAX_TOKENS * _BYTES_PER_TOKEN  # 32 KiB


class BlastRadiusRefused(RuntimeError):
    """A prune would remove more of the bucket than the caller allowed.

    Raised *before* any mutation — no upload, no delete, no ingestion job — so a
    refused sync leaves the bucket exactly as it was.
    """

    def __init__(self, *, deletions: int, remote_total: int, ratio: float, limit: float) -> None:
        self.deletions = deletions
        self.remote_total = remote_total
        self.ratio = ratio
        self.limit = limit
        super().__init__(
            f"refusing to prune {deletions} of {remote_total} keys under the prefix "
            f"({ratio:.0%} > the {limit:.0%} blast-radius limit); nothing was changed.\n"
            "  The usual cause is a local corpus that is not the one you think it is: an\n"
            "  unbuilt or half-built build/kb, or a --source pointing one level too deep.\n"
            "  Rebuild with `pnpm kb:build` and re-run.\n"
            "  If this many chunks really are retiring, say the number out loud:\n"
            "  --max-delete-ratio 1.0 (or any value above the ratio printed above)."
        )


class OversizedDocRefused(ValueError):
    """A document likely exceeds the embedding model's token cap.

    Raised at plan time — before any upload — because with one-file-one-vector
    the alternative is a FAILED document surfacing mid-ingestion. Sidecars are
    exempt: they produce no vector, and their 2 KB filterable-metadata cap is
    Bedrock's own constraint, enforced at ingestion.
    """

    def __init__(self, docs: list[tuple[str, int]], limit: int) -> None:
        self.docs = docs
        self.limit = limit
        listing = "\n".join(f"  {path} — {size:,} B" for path, size in docs)
        super().__init__(
            f"{len(docs)} document(s) exceed {limit:,} B (≈{limit // _BYTES_PER_TOKEN:,} tokens "
            f"at {_BYTES_PER_TOKEN} B/token; Titan v2 caps at {TITAN_V2_MAX_TOKENS:,}); "
            "nothing was uploaded.\n"
            f"{listing}\n"
            "  Split the source document upstream (`pnpm kb:build`) so each chunk embeds\n"
            "  whole, or — if the file is measured to fit anyway — raise max_doc_bytes\n"
            "  (--max-doc-bytes) above the size printed above."
        )


class SyncAction(StrEnum):
    UPLOADED = "uploaded"
    SKIPPED = "skipped"  # identical bytes already present in S3


class PlannedDoc(BaseModel):
    path: str  # local filesystem path, for logs
    key: str  # S3 object key
    digest: str  # MD5 hex of the local bytes
    size: int
    action: SyncAction
    # True for a `<file>.metadata.json` companion. It is not a document in its
    # own right — it produces no vector — so callers list documents and count
    # sidecars separately rather than reporting 766 "docs" for a 383-doc corpus.
    sidecar: bool = False


class PrunedKey(BaseModel):
    """A key in the bucket that the local build no longer produces.

    No local path, no digest — the file it mirrored does not exist any more.
    That is why it is not a `PlannedDoc`.
    """

    key: str
    sidecar: bool


class IngestionOutcome(BaseModel):
    job_id: str
    status: str
    documents_scanned: int | None = None
    documents_indexed: int | None = None
    documents_failed: int | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "COMPLETE"


class SyncReport(BaseModel):
    docs: list[PlannedDoc]
    pruned: list[PrunedKey] = []
    ingestion: IngestionOutcome | None = None

    @property
    def uploaded(self) -> list[PlannedDoc]:
        return [d for d in self.docs if d.action is SyncAction.UPLOADED]

    @property
    def skipped(self) -> list[PlannedDoc]:
        return [d for d in self.docs if d.action is SyncAction.SKIPPED]

    @property
    def documents(self) -> list[PlannedDoc]:
        """Everything that becomes a vector — i.e. excluding metadata sidecars."""
        return [d for d in self.docs if not d.sidecar]

    @property
    def sidecars(self) -> list[PlannedDoc]:
        return [d for d in self.docs if d.sidecar]


def discover_corpus(root: Path) -> list[Path]:
    """Every ``*.md`` under ``root``, plus each one's metadata sidecar.

    A sidecar is only collected when its ``.md`` is itself in the corpus: an
    orphan ``foo.md.metadata.json`` with no ``foo.md`` describes nothing, and
    uploading it would leave a file in the bucket that no ingestion ever reads.
    Sorted so keys and logs are deterministic.
    """
    docs = sorted(p for p in root.rglob("*.md") if p.is_file())
    found: list[Path] = []
    for doc in docs:
        found.append(doc)
        sidecar = doc.with_name(doc.name + _SIDECAR_SUFFIX)
        if sidecar.is_file():
            found.append(sidecar)
    return found


def is_sidecar(path: Path) -> bool:
    return path.name.endswith(_SIDECAR_SUFFIX)


def prune_order(keys: set[str]) -> list[str]:
    """Order a delete set so a document is always removed before its sidecar.

    The order is a safety property, not a cosmetic one. A `.md` left in the
    bucket *without* its `.metadata.json` is a silent defect: Bedrock re-embeds
    it with no attributes, so an unrated chunk sails straight through a
    `min_evidence` filter downstream (`retrieval.py`). The reverse — a sidecar
    whose document is gone — is inert: no ingestion reads it, which is the
    orphan `discover_corpus` already refuses to create locally. So if a prune
    dies half-way through, it must die on the inert side.

    Plain `sorted()` happens to produce this order today (a string sorts before
    any string it prefixes), but that is a property of the collation, not a
    decision. This says it on purpose.
    """
    ordered: list[str] = []
    for key in sorted(keys):
        if key.endswith(_SIDECAR_SUFFIX) and key[: -len(_SIDECAR_SUFFIX)] in keys:
            continue  # emitted just below, right after its document
        ordered.append(key)
        sidecar = key + _SIDECAR_SUFFIX
        if sidecar in keys:
            ordered.append(sidecar)
    return ordered


def _digest(data: bytes) -> str:
    # Change detection only — not security. Matches S3's ETag for single-part,
    # SSE-S3 (AES256) objects, which is exactly what the kb module provisions.
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


class CorpusSyncer:
    """Uploads the local corpus to the KB source bucket and (re)ingests it."""

    def __init__(
        self,
        source_bucket: str,
        knowledge_base_id: str,
        data_source_id: str,
        s3_client: Any = None,
        agent_client: Any = None,
        prefix: str = DEFAULT_PREFIX,
        logger: Any = None,
    ) -> None:
        self.bucket = source_bucket
        self.knowledge_base_id = knowledge_base_id
        self.data_source_id = data_source_id
        self.prefix = prefix
        self._s3 = s3_client or boto3.client("s3")
        self._agent = agent_client or boto3.client("bedrock-agent")
        self._log = logger or get_logger(__name__)

    def key_for(self, root: Path, path: Path) -> str:
        """S3 key = prefix + the file's path relative to the corpus root."""
        return f"{self.prefix}{path.relative_to(root).as_posix()}"

    def plan(self, root: Path, *, max_doc_bytes: int = DEFAULT_MAX_DOC_BYTES) -> list[PlannedDoc]:
        """What ``sync`` would do — upload vs skip per file, no mutations.

        Refuses (``OversizedDocRefused``) when any document is likely past the
        embedding model's token cap, naming every offender at once rather than
        failing them one ingestion at a time.
        """
        planned: list[PlannedDoc] = []
        oversized: list[tuple[str, int]] = []
        for path in discover_corpus(root):
            data = path.read_bytes()
            digest = _digest(data)
            key = self.key_for(root, path)
            if not is_sidecar(path) and len(data) > max_doc_bytes:
                oversized.append((str(path), len(data)))
            action = (
                SyncAction.SKIPPED if self._matches_remote(key, digest) else SyncAction.UPLOADED
            )
            planned.append(
                PlannedDoc(
                    path=str(path),
                    key=key,
                    digest=digest,
                    size=len(data),
                    action=action,
                    sidecar=is_sidecar(path),
                )
            )
        if oversized:
            raise OversizedDocRefused(oversized, max_doc_bytes)
        return planned

    def list_remote_keys(self) -> set[str]:
        """Every key currently under the prefix. Paginated — the corpus is 766+."""
        keys: set[str] = set()
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            keys.update(obj["Key"] for obj in page.get("Contents", []))
        return keys

    def plan_prune(self, root: Path | str) -> list[PrunedKey]:
        """Remote minus local: what a sync would delete, deleting nothing.

        This is also the audit: run it against a live bucket and the length is
        the orphan count, the keys are the orphans.
        """
        return self._prune_diff(Path(root))[0]

    def _prune_diff(self, root: Path) -> tuple[list[PrunedKey], int]:
        """(what to delete, how many keys are under the prefix at all).

        The second number is the blast-radius denominator, so it is read from
        the same listing as the first — a ratio computed against a stale count
        is not a guard.
        """
        local = {self.key_for(root, path) for path in discover_corpus(root)}
        remote = self.list_remote_keys()
        orphans = prune_order(remote - local)
        return (
            [PrunedKey(key=k, sidecar=k.endswith(_SIDECAR_SUFFIX)) for k in orphans],
            len(remote),
        )

    def _matches_remote(self, key: str, digest: str) -> bool:
        try:
            head = self._s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING_OBJECT_CODES:
                return False
            raise
        # Multipart uploads carry a "-N" suffix and won't match a plain MD5 —
        # that just means we re-upload (safe), never a false "skip".
        return head.get("ETag", "").strip('"') == digest

    def sync(
        self,
        root: Path | str,
        *,
        start_ingestion: bool = True,
        wait: bool = True,
        prune: bool = True,
        max_delete_ratio: float = DEFAULT_MAX_DELETE_RATIO,
        max_doc_bytes: int = DEFAULT_MAX_DOC_BYTES,
        poll_interval: float = 5.0,
        timeout: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> SyncReport:
        """Make the bucket equal ``root`` — upload what changed, delete what left.

        ``prune=False`` restores the append-only behaviour this module had
        before 2026-08-14, kept reachable and named rather than removed.
        ``max_delete_ratio=1.0`` disables the blast-radius guard for a
        deliberate large reshape.
        """
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"corpus root is not a directory: {root}")

        planned = self.plan(root, max_doc_bytes=max_doc_bytes)
        # The whole diff is computed, and the guard consulted, before the first
        # mutation. A refusal means "this local tree is not trustworthy", and a
        # tree that cannot be trusted to delete cannot be trusted to upload
        # either — so a refused sync leaves the bucket byte-for-byte as it was.
        to_prune: list[PrunedKey] = []
        if prune:
            to_prune, remote_total = self._prune_diff(root)
            self._check_blast_radius(to_prune, remote_total, max_delete_ratio)

        for doc in planned:
            if doc.action is SyncAction.UPLOADED:
                self._s3.put_object(
                    Bucket=self.bucket,
                    Key=doc.key,
                    Body=Path(doc.path).read_bytes(),
                    ContentType=(_JSON_CONTENT_TYPE if doc.sidecar else _MARKDOWN_CONTENT_TYPE),
                )
        self._log.info(
            "corpus uploaded",
            extra={
                "context": {
                    "bucket": self.bucket,
                    "uploaded": len([d for d in planned if d.action is SyncAction.UPLOADED]),
                    "skipped": len([d for d in planned if d.action is SyncAction.SKIPPED]),
                    "sidecars": len([d for d in planned if d.sidecar]),
                }
            },
        )
        self._prune(to_prune)

        if not start_ingestion:
            return SyncReport(docs=planned, pruned=to_prune)

        outcome = self._ingest(
            wait=wait, poll_interval=poll_interval, timeout=timeout, sleep=sleep, clock=clock
        )
        return SyncReport(docs=planned, pruned=to_prune, ingestion=outcome)

    def _check_blast_radius(
        self, to_prune: list[PrunedKey], remote_total: int, limit: float
    ) -> None:
        if not to_prune:
            return
        ratio = len(to_prune) / remote_total  # remote_total >= len(to_prune) >= 1
        if ratio > limit:
            raise BlastRadiusRefused(
                deletions=len(to_prune), remote_total=remote_total, ratio=ratio, limit=limit
            )

    def _prune(self, to_prune: list[PrunedKey]) -> None:
        """Delete orphaned keys one at a time, in ``prune_order``.

        Deliberately not ``delete_objects`` (the 1000-key batch call): S3 gives
        no ordering guarantee within a batch, and the document-before-sidecar
        order is the point. At corpus scale this is ≤ 766 calls in the absolute
        worst case, and the guard makes that case unreachable without a flag.
        """
        if not to_prune:
            return
        for target in to_prune:
            self._s3.delete_object(Bucket=self.bucket, Key=target.key)
        self._log.info(
            "orphans pruned",
            extra={
                "context": {
                    "bucket": self.bucket,
                    "deleted": len(to_prune),
                    "documents": len([p for p in to_prune if not p.sidecar]),
                    "keys": [p.key for p in to_prune],
                }
            },
        )

    def _ingest(
        self,
        *,
        wait: bool,
        poll_interval: float,
        timeout: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> IngestionOutcome:
        started = self._agent.start_ingestion_job(
            knowledgeBaseId=self.knowledge_base_id,
            dataSourceId=self.data_source_id,
        )
        job = started["ingestionJob"]
        job_id = job["ingestionJobId"]
        status = job["status"]
        self._log.info("ingestion started", extra={"context": {"job_id": job_id}})

        if wait:
            job, status = self._poll(job_id, status, poll_interval, timeout, sleep, clock)

        stats = job.get("statistics", {})
        outcome = IngestionOutcome(
            job_id=job_id,
            status=status,
            documents_scanned=stats.get("numberOfDocumentsScanned"),
            documents_indexed=stats.get("numberOfNewDocumentsIndexed"),
            documents_failed=stats.get("numberOfDocumentsFailed"),
        )
        self._log.info(
            "ingestion finished",
            extra={"context": {"job_id": job_id, "status": status}},
        )
        return outcome

    def _poll(
        self,
        job_id: str,
        status: str,
        poll_interval: float,
        timeout: float,
        sleep: Callable[[float], None],
        clock: Callable[[], float],
    ) -> tuple[dict[str, Any], str]:
        job: dict[str, Any] = {}
        deadline = clock() + timeout
        while status not in _TERMINAL_STATUSES:
            if clock() > deadline:
                return job, _TIMEOUT_STATUS
            sleep(poll_interval)
            got = self._agent.get_ingestion_job(
                knowledgeBaseId=self.knowledge_base_id,
                dataSourceId=self.data_source_id,
                ingestionJobId=job_id,
            )
            job = got["ingestionJob"]
            status = job["status"]
        return job, status
