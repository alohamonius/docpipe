"""What a corpus *is* on disk: discovery, sidecar pairing, prune ordering.

Pure functions over the local tree — no AWS clients here, which is what lets
`discover_corpus` double as the audit primitive (`plan_prune` is remote minus
this).
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_PREFIX = "corpus/"

# `pnpm kb:build` writes this at the corpus root; its mtime is "when the corpus
# was built", which is what every input is checked against.
MANIFEST_NAME = "MANIFEST.json"

# Directories under the health.studio checkout whose contents can change what
# `pnpm kb:build` emits: the prose corpus, the chunker, and the body graph.
CORPUS_INPUTS = ("docs", "apps/web/src/lib/kb", "apps/web/src/lib/body")

# Test files live beside the sources they cover but contribute nothing to the
# build output. Counting them makes the staleness warning cry wolf on every
# test-only commit, and a warning that is usually wrong gets ignored when it
# is right.
_NOT_CORPUS_INPUT = (".test.ts", ".test.tsx", ".test.mts")

# Bedrock's S3 data source attaches `<file>.metadata.json` to every vector it
# derives from `<file>`. health.studio ships one per chunk (docTitle, section,
# maxEvidence, verification, citationCount, safetyCritical, …) — without them
# there is no retrieval-time filtering and a citation is a bare S3 URI.
_SIDECAR_SUFFIX = ".metadata.json"


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


def feeds_the_corpus(relative_to_docs: str, manifest: dict[str, Any]) -> bool:
    """Whether a path under ``docs/`` is actually an input to the build.

    Not every file under `docs/` becomes a chunk, and the manifest says which:
    `allowedDirectories` is the allowlist and `excludedDocs` names the files
    ruled out individually (build policy, licences, market research). Guessing
    at directory level instead gets `docs/FINDINGS.md` wrong — an engineering
    log that is edited after almost every session and produces **zero** chunks
    ("root-level doc — not in an allowlisted knowledge directory").

    That matters more than one wrong filename. This repo's convention is to
    append findings to `FINDINGS.md` continuously, so a directory-level check
    fires a stale-corpus warning on nearly every sync while being right almost
    never — and a warning that cries wolf is one nobody reads on the day it is
    correct.

    Falls back to "yes" when the manifest predates these keys, so an older
    build over-warns rather than staying silent about a genuinely stale corpus.
    """
    allowed = manifest.get("allowedDirectories")
    if not isinstance(allowed, list) or not allowed:
        return True
    path = PurePosixPath(relative_to_docs)
    excluded = {
        entry["path"]
        for entry in manifest.get("excludedDocs") or []
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    if str(path) in excluded:
        return False
    return bool(path.parts) and path.parts[0] in allowed


def stale_inputs(root: Path, manifest: dict[str, Any]) -> list[str]:
    """Names of corpus inputs modified after the manifest was written.

    Best-effort: the corpus can be synced from anywhere, so if the health.studio
    checkout isn't where we expect relative to ``root``, this says nothing at all
    rather than guessing. `build/kb` is gitignored, so a stale artifact is the
    normal state of a machine that has pulled but not rebuilt.
    """
    # build/kb → build → the repo root. A corpus can legitimately live anywhere
    # (a tarball, /tmp, a CI artifact), so anything that isn't shaped like the
    # health.studio checkout gets no opinion rather than a guess or a crash.
    parents = root.resolve().parents
    if len(parents) < 2:
        return []
    repo = parents[1]
    if not all((repo / d).is_dir() for d in CORPUS_INPUTS):
        return []

    built_at = (root / MANIFEST_NAME).stat().st_mtime
    stale: list[str] = []
    for directory in CORPUS_INPUTS:
        for path in (repo / directory).rglob("*"):
            if path.name.endswith(_NOT_CORPUS_INPUT) or not path.is_file():
                continue
            relative = path.relative_to(repo)
            if directory == "docs" and not feeds_the_corpus(
                path.relative_to(repo / "docs").as_posix(), manifest
            ):
                continue
            if path.stat().st_mtime > built_at:
                stale.append(str(relative))
    return sorted(stale)
