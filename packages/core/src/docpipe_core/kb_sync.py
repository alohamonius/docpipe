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
  * **Ingestion is incremental on Bedrock's side.** `StartIngestionJob`
    re-embeds only added/changed/deleted documents, so re-running after editing
    one doc is cheap — you don't pay to re-embed the whole corpus.
  * **Metadata sidecars ride along.** A `<file>.md.metadata.json` next to a
    document is uploaded with it, and Bedrock attaches its attributes to every
    vector from that document. That is what makes `min_evidence` filtering and
    named citations possible downstream (see `retrieval.py`).
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
    # sidecars separately rather than reporting 570 "docs" for a 285-doc corpus.
    sidecar: bool = False


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

    def plan(self, root: Path) -> list[PlannedDoc]:
        """What ``sync`` would do — upload vs skip per file, no mutations."""
        planned: list[PlannedDoc] = []
        for path in discover_corpus(root):
            data = path.read_bytes()
            digest = _digest(data)
            key = self.key_for(root, path)
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
        return planned

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
        poll_interval: float = 5.0,
        timeout: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> SyncReport:
        root = Path(root)
        if not root.is_dir():
            raise ValueError(f"corpus root is not a directory: {root}")

        planned = self.plan(root)
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

        if not start_ingestion:
            return SyncReport(docs=planned)

        outcome = self._ingest(
            wait=wait, poll_interval=poll_interval, timeout=timeout, sleep=sleep, clock=clock
        )
        return SyncReport(docs=planned, ingestion=outcome)

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
