"""The mirror itself: plan → guard → upload → prune → ingest, in that order.

Two AWS clients here, deliberately different — easy to conflate:
  * ``s3``            — uploads the markdown files.
  * ``bedrock-agent`` — the *control-plane* client that starts/polls the
    ingestion job. (Retrieval uses ``bedrock-agent-runtime``; ingestion does
    not. Same "bedrock-agent" family name, different endpoint.)
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

from docpipe_core.kb_sync.corpus import (
    _SIDECAR_SUFFIX,
    DEFAULT_PREFIX,
    discover_corpus,
    is_sidecar,
    prune_order,
)
from docpipe_core.kb_sync.guards import (
    DEFAULT_MAX_DELETE_RATIO,
    DEFAULT_MAX_DOC_BYTES,
    BlastRadiusRefused,
    OversizedDocRefused,
)
from docpipe_core.observability import get_logger

_MARKDOWN_CONTENT_TYPE = "text/markdown"
_JSON_CONTENT_TYPE = "application/json"

# Bedrock ingestion-job lifecycle: STARTING → IN_PROGRESS → one of these.
_TERMINAL_STATUSES = frozenset({"COMPLETE", "FAILED", "STOPPED"})
# Set locally when we stop waiting; never returned by Bedrock.
_TIMEOUT_STATUS = "TIMEOUT"

# HeadObject on a missing key surfaces as one of these error codes (HEAD has no
# body, so S3 returns bare "404"; moto/other paths may say NoSuchKey/NotFound).
_MISSING_OBJECT_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


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
