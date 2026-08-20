"""What a corpus *is* on disk: discovery, sidecar pairing, prune ordering.

Pure functions over the local tree — no AWS clients here, which is what lets
`discover_corpus` double as the audit primitive (`plan_prune` is remote minus
this).
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_PREFIX = "corpus/"

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
