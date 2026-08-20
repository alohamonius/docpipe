"""CLI: sync health.studio's public corpus into the Bedrock Knowledge Base.

Thin wrapper around ``docpipe_core.kb_sync.CorpusSyncer`` (all the logic + tests
live there). This file only resolves configuration, guards the corpus root, and
prints a summary.

**``--source`` is ``build/kb``, never ``docs``.** health.studio's `docs/` tree is
not the corpus; `build/kb/` — produced by ``pnpm kb:build`` — is. The difference
is not cosmetic. `docs/` holds files the corpus policy
(`apps/web/src/lib/kb/corpus.ts`) excludes by name, including `PM.md` (uncited
market research that would retrieve as sourced fact) and
`anatomy/referral-boundary.md`, which is held out for a **safety** reason: to
forbid a dangerous sentence it must quote it, and a retrieved fragment cannot
tell the quotation from the claim. This CLI refuses such a root outright.

The bucket / KB id / data-source id are resolved in priority order:
  1. explicit flags (``--bucket`` / ``--knowledge-base-id`` / ``--data-source-id``),
  2. environment variables (``KB_SOURCE_BUCKET`` / ``KNOWLEDGE_BASE_ID`` /
     ``KB_DATA_SOURCE_ID``),
  3. ``pulumi stack output --json`` on the dev stack — so right after
     ``pulumi up`` you can run this with just ``--source``.

Examples:
    # rebuild the corpus first — this CLI never does it for you
    (cd ../health.studio && pnpm kb:build)

    # dry run — show what would upload, touch nothing, no AWS writes
    uv run python services/kb_sync/sync.py --source ../health.studio/build/kb --dry-run

    # full sync: upload changed docs, start ingestion, wait for it to finish
    uv run python services/kb_sync/sync.py --source ../health.studio/build/kb
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

from docpipe_core.kb_sync import (
    DEFAULT_MAX_DELETE_RATIO,
    DEFAULT_MAX_DOC_BYTES,
    BlastRadiusRefused,
    CorpusSyncer,
    OversizedDocRefused,
)

# Default location of the Pulumi program, relative to the repo root.
_DEFAULT_PULUMI_DIR = Path(__file__).resolve().parents[2] / "pulumi"

# Written by `pnpm kb:build`; its presence is what makes a directory "a built
# corpus" rather than "some markdown".
_MANIFEST = "MANIFEST.json"

# Files that only exist in health.studio's `docs/` tree. Finding one means the
# operator pointed at the source docs instead of the build output. Each is
# excluded by `corpus.ts`, and the second is excluded for safety.
_DOCS_TREE_MARKERS = ("PM.md", "positioning-kit.md", "anatomy/referral-boundary.md")

# Directories whose edits invalidate a built corpus: the prose it chunks, and
# the TypeScript the 93 graph chunks are generated from.
_CORPUS_INPUTS = ("docs", "apps/web/src/lib/kb", "apps/web/src/lib/body")

# Test files live beside the sources they cover but contribute nothing to the
# build output. Counting them makes the staleness warning cry wolf on every
# test-only commit, and a warning that is usually wrong gets ignored when it
# is right.
_NOT_CORPUS_INPUT = (".test.ts", ".test.tsx", ".test.mts")

# (flag dest, env var, pulumi stack output name)
_SETTINGS = (
    ("bucket", "KB_SOURCE_BUCKET", "kb_source_bucket"),
    ("knowledge_base_id", "KNOWLEDGE_BASE_ID", "knowledge_base_id"),
    ("data_source_id", "KB_DATA_SOURCE_ID", "kb_data_source_id"),
)


@cache
def _pulumi_outputs(pulumi_dir: str) -> dict[str, object]:
    """Parse ``pulumi stack output --json``; empty dict if pulumi can't run."""
    env = {**os.environ}
    env.setdefault("PULUMI_CONFIG_PASSPHRASE", "")
    try:
        result = subprocess.run(
            ["pulumi", "stack", "output", "--json", "--cwd", pulumi_dir],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError):
        return {}
    return json.loads(result.stdout or "{}")


def resolve_config(args: argparse.Namespace) -> dict[str, str]:
    """Flags → env vars → pulumi stack output. Errors listing whatever's missing."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for dest, env_var, output_name in _SETTINGS:
        value = getattr(args, dest) or os.environ.get(env_var)
        if not value:
            value = _pulumi_outputs(args.pulumi_dir).get(output_name)
        if value:
            resolved[dest] = str(value)
        else:
            flag = "--" + dest.replace("_", "-")
            missing.append(f"{flag} (or ${env_var}, or stack output {output_name})")
    if missing:
        raise SystemExit("Could not resolve required settings:\n  " + "\n  ".join(missing))
    return resolved


def assert_built_corpus(root: Path) -> dict:
    """Refuse anything that isn't the output of ``pnpm kb:build``.

    Two failure modes, deliberately separated so the error names the actual
    mistake. Pointing at `docs/` is the dangerous one and is checked first.
    """
    offenders = [m for m in _DOCS_TREE_MARKERS if (root / m).is_file()]
    if offenders:
        raise SystemExit(
            f"refusing to sync {root}: this is health.studio's docs/ tree, not the corpus.\n"
            f"  found {', '.join(offenders)} — excluded by apps/web/src/lib/kb/corpus.ts.\n"
            "  `referral-boundary.md` in particular is held out for SAFETY: it quotes the\n"
            "  sentence it exists to forbid, and retrieval cannot tell the two apart.\n"
            "  Run `pnpm kb:build` in health.studio and point --source at build/kb."
        )

    manifest_path = root / _MANIFEST
    if not manifest_path.is_file():
        raise SystemExit(
            f"refusing to sync {root}: no {_MANIFEST} — this is not a built corpus.\n"
            "  Run `pnpm kb:build` in health.studio and point --source at build/kb."
        )
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"{manifest_path} is unreadable: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SystemExit(f"{manifest_path} is not a JSON object")
    return manifest


def warn_if_stale(root: Path) -> list[str]:
    """Names of corpus inputs modified after the manifest was written.

    Best-effort: the corpus can be synced from anywhere, so if the health.studio
    checkout isn't where we expect relative to ``root``, this says nothing at all
    rather than guessing. `build/kb` is gitignored, so a stale artifact is the
    normal state of a machine that has pulled but not rebuilt.
    """
    manifest_path = root / _MANIFEST
    # build/kb → build → the repo root. A corpus can legitimately live anywhere
    # (a tarball, /tmp, a CI artifact), so anything that isn't shaped like the
    # health.studio checkout gets no opinion rather than a guess or a crash.
    parents = root.resolve().parents
    if len(parents) < 2:
        return []
    repo = parents[1]
    if not all((repo / d).is_dir() for d in _CORPUS_INPUTS):
        return []

    built_at = manifest_path.stat().st_mtime
    stale: list[str] = []
    for directory in _CORPUS_INPUTS:
        for path in (repo / directory).rglob("*"):
            if path.name.endswith(_NOT_CORPUS_INPUT) or not path.is_file():
                continue
            if path.stat().st_mtime > built_at:
                stale.append(str(path.relative_to(repo)))
    return sorted(stale)


def _print_manifest(manifest: dict, root: Path) -> None:
    totals = manifest.get("totals", {})
    histogram = manifest.get("evidenceHistogram", {})
    print(f"corpus: {root}")
    print(
        f"  {totals.get('chunks', '?')} chunks "
        f"({totals.get('proseChunks', '?')} prose from {totals.get('sourceDocs', '?')} docs, "
        f"{totals.get('graphChunks', '?')} graph)"
    )
    if histogram:
        print(
            f"  evidence: ★★★ {histogram.get('3', 0)}  ★★☆ {histogram.get('2', 0)}  "
            f"★☆☆ {histogram.get('1', 0)}  unrated {histogram.get('0', 0)}"
        )
    if (uncited := manifest.get("chunksWithNoCitation")) is not None:
        print(f"  chunks with no citation: {uncited}")


def _print_prune(pruned, dry_run: bool) -> None:
    """Deletions get their own block. They are the irreversible half."""
    if not pruned:
        print("\norphans in the bucket: none — it already equals this build")
        return
    verb = "would delete" if dry_run else "deleted"
    documents = [p for p in pruned if not p.sidecar]
    print(f"\n{verb}: {len(pruned)} orphaned key(s) — {len(documents)} document(s) + sidecars")
    for doc in documents:
        print(f"  − {doc.key}")


def _print_report(report, dry_run: bool) -> None:
    verb = "would upload" if dry_run else "uploaded"
    print(f"\n{verb}: {len(report.uploaded)}   unchanged (skipped): {len(report.skipped)}")
    # Only documents are listed. Sidecars ride along with their parent and would
    # otherwise double the output for no information.
    for doc in report.documents:
        marker = "＋" if doc.action.value == "uploaded" else "·"
        print(f"  {marker} {doc.key}  ({doc.size} B)")
    sidecars = report.sidecars
    if sidecars:
        changed = len([d for d in sidecars if d.action.value == "uploaded"])
        print(f"  + {len(sidecars)} metadata sidecars ({changed} {verb})")
    else:
        print("  ! no metadata sidecars found — retrieval will have no evidence grades")
    if report.ingestion is not None:
        ing = report.ingestion
        print(f"\ningestion job {ing.job_id}: {ing.status}")
        if ing.documents_indexed is not None:
            print(
                f"  scanned={ing.documents_scanned} "
                f"indexed={ing.documents_indexed} failed={ing.documents_failed}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="corpus root (e.g. ../health.studio/docs)")
    parser.add_argument("--bucket", help="KB source S3 bucket (else env / stack output)")
    parser.add_argument("--knowledge-base-id", dest="knowledge_base_id")
    parser.add_argument("--data-source-id", dest="data_source_id")
    parser.add_argument("--prefix", default="corpus/", help="S3 key prefix (default: corpus/)")
    parser.add_argument("--pulumi-dir", dest="pulumi_dir", default=str(_DEFAULT_PULUMI_DIR))
    parser.add_argument(
        "--dry-run", action="store_true", help="show the upload plan; write nothing"
    )
    parser.add_argument(
        "--no-ingest", action="store_true", help="upload but don't start an ingestion job"
    )
    parser.add_argument(
        "--no-wait", action="store_true", help="start ingestion but don't poll to completion"
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="leave keys the build no longer produces in the bucket (append-only, pre-2026-08-14)",
    )
    parser.add_argument(
        "--max-delete-ratio",
        dest="max_delete_ratio",
        type=float,
        default=DEFAULT_MAX_DELETE_RATIO,
        help=(
            "refuse the sync if a prune would remove more than this fraction of the "
            f"keys under the prefix (default {DEFAULT_MAX_DELETE_RATIO:.0%}; 1.0 disables)"
        ),
    )
    parser.add_argument(
        "--max-doc-bytes",
        dest="max_doc_bytes",
        type=int,
        default=DEFAULT_MAX_DOC_BYTES,
        help=(
            "refuse the sync if any document exceeds this size — a proxy for Titan v2's "
            f"8,192-token embedding cap (default {DEFAULT_MAX_DOC_BYTES:,} B)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.source)
    if not root.is_dir():
        raise SystemExit(f"--source is not a directory: {root}")

    manifest = assert_built_corpus(root)
    _print_manifest(manifest, root)
    if stale := warn_if_stale(root):
        shown = ", ".join(stale[:5])
        more = f" (+{len(stale) - 5} more)" if len(stale) > 5 else ""
        print(
            f"\n  ⚠ {len(stale)} corpus input(s) modified since the corpus was built: "
            f"{shown}{more}\n"
            "    Run `pnpm kb:build` in health.studio first, or you will ingest stale text."
        )

    cfg = resolve_config(args)
    syncer = CorpusSyncer(
        cfg["bucket"],
        knowledge_base_id=cfg["knowledge_base_id"],
        data_source_id=cfg["data_source_id"],
        prefix=args.prefix,
    )

    if args.dry_run:
        # plan()/plan_prune() read local files + list/HEAD S3, and never mutate.
        try:
            report_docs = syncer.plan(root, max_doc_bytes=args.max_doc_bytes)
        except OversizedDocRefused as refused:
            print(f"\n⛔ {refused}", file=sys.stderr)
            return 2
        from docpipe_core.kb_sync import SyncReport

        _print_report(SyncReport(docs=report_docs), dry_run=True)
        if not args.no_prune:
            _print_prune(syncer.plan_prune(root), dry_run=True)
        return 0

    try:
        report = syncer.sync(
            root,
            start_ingestion=not args.no_ingest,
            wait=not args.no_wait,
            prune=not args.no_prune,
            max_delete_ratio=args.max_delete_ratio,
            max_doc_bytes=args.max_doc_bytes,
        )
    except (BlastRadiusRefused, OversizedDocRefused) as refused:
        # Not a crash — a guard doing its job. Say so, and say it loudly.
        print(f"\n⛔ {refused}", file=sys.stderr)
        return 2
    _print_report(report, dry_run=False)
    if not args.no_prune:
        _print_prune(report.pruned, dry_run=False)
    if report.ingestion is not None and not report.ingestion.succeeded and not args.no_wait:
        return 1  # ingestion FAILED / TIMEOUT — surface a non-zero exit for CI
    return 0


if __name__ == "__main__":
    sys.exit(main())
