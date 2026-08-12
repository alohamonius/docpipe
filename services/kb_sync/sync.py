"""CLI: sync health.studio's public corpus into the Bedrock Knowledge Base.

Thin wrapper around ``docpipe_core.kb_sync.CorpusSyncer`` (all the logic + tests
live there). This file only resolves configuration and prints a summary.

The bucket / KB id / data-source id are resolved in priority order:
  1. explicit flags (``--bucket`` / ``--knowledge-base-id`` / ``--data-source-id``),
  2. environment variables (``KB_SOURCE_BUCKET`` / ``KNOWLEDGE_BASE_ID`` /
     ``KB_DATA_SOURCE_ID``),
  3. ``pulumi stack output --json`` on the dev stack — so right after
     ``pulumi up`` you can run this with just ``--source``.

Examples:
    # dry run — show what would upload, touch nothing, no AWS writes
    uv run python services/kb_sync/sync.py --source ../health.studio/docs --dry-run

    # full sync: upload changed docs, start ingestion, wait for it to finish
    uv run python services/kb_sync/sync.py --source ../health.studio/docs
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

from docpipe_core.kb_sync import CorpusSyncer

# Default location of the Pulumi program, relative to the repo root.
_DEFAULT_PULUMI_DIR = Path(__file__).resolve().parents[2] / "pulumi"

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


def _print_report(report, dry_run: bool) -> None:
    verb = "would upload" if dry_run else "uploaded"
    print(f"\n{verb}: {len(report.uploaded)}   unchanged (skipped): {len(report.skipped)}")
    for doc in report.docs:
        marker = "＋" if doc.action.value == "uploaded" else "·"
        print(f"  {marker} {doc.key}  ({doc.size} B)")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.source)
    if not root.is_dir():
        raise SystemExit(f"--source is not a directory: {root}")

    cfg = resolve_config(args)
    syncer = CorpusSyncer(
        cfg["bucket"],
        knowledge_base_id=cfg["knowledge_base_id"],
        data_source_id=cfg["data_source_id"],
        prefix=args.prefix,
    )

    if args.dry_run:
        # plan() reads local files + HEADs S3 but never uploads or ingests.
        report_docs = syncer.plan(root)
        from docpipe_core.kb_sync import SyncReport

        _print_report(SyncReport(docs=report_docs), dry_run=True)
        return 0

    report = syncer.sync(root, start_ingestion=not args.no_ingest, wait=not args.no_wait)
    _print_report(report, dry_run=False)
    if report.ingestion is not None and not report.ingestion.succeeded and not args.no_wait:
        return 1  # ingestion FAILED / TIMEOUT — surface a non-zero exit for CI
    return 0


if __name__ == "__main__":
    sys.exit(main())
