"""Sync health.studio's public corpus into the Bedrock Knowledge Base.

Pipeline (mirrors the `kb` Pulumi component): local markdown → the KB **source** S3
bucket → `StartIngestionJob` → Bedrock chunks each file and embeds it with
Titan v2 (1024-dim) → the vectors land in the S3 Vectors index that
`KnowledgeBaseClient.retrieve()` later queries at chat time.

Split by concern (2026-08-21) — one module per question:

  * ``corpus``  — what a corpus *is* on disk: discovery, sidecar pairing,
    prune ordering. Pure functions, no AWS.
  * ``guards``  — every way a sync refuses before the first mutation:
    the blast-radius limit and the oversized-document cap.
  * ``syncer``  — the mirror itself: plan → guard → upload → prune → ingest.

The public surface is re-exported here, so ``from docpipe_core.kb_sync
import CorpusSyncer`` is unchanged from when this package was one file.

Everything is dependency-injected, so the whole package is unit-tested with
moto (S3) + a fake agent client — no AWS, no spend. Properties worth knowing:

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

from docpipe_core.kb_sync.corpus import (
    DEFAULT_PREFIX,
    discover_corpus,
    is_sidecar,
    prune_order,
)
from docpipe_core.kb_sync.guards import (
    DEFAULT_MAX_DELETE_RATIO,
    DEFAULT_MAX_DOC_BYTES,
    TITAN_V2_MAX_TOKENS,
    BlastRadiusRefused,
    OversizedDocRefused,
)
from docpipe_core.kb_sync.syncer import (
    CorpusSyncer,
    IngestionOutcome,
    PlannedDoc,
    PrunedKey,
    SyncAction,
    SyncReport,
)

__all__ = [
    "DEFAULT_MAX_DELETE_RATIO",
    "DEFAULT_MAX_DOC_BYTES",
    "DEFAULT_PREFIX",
    "TITAN_V2_MAX_TOKENS",
    "BlastRadiusRefused",
    "CorpusSyncer",
    "IngestionOutcome",
    "OversizedDocRefused",
    "PlannedDoc",
    "PrunedKey",
    "SyncAction",
    "SyncReport",
    "discover_corpus",
    "is_sidecar",
    "prune_order",
]
