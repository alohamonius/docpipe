"""Build the body-graph edge index from the corpus — PLAN Phase 5b, rung (a).

A thin shell over ``docpipe_core.graph``: it reads markdown off disk, hands it
to ``build_graph`` as ``{chunk_key: text}``, and writes the result. All parsing
and traversal logic lives in core, with tests.

    make graph-index                      # from the DEPLOYED corpus in S3
    make graph-index SOURCE=/path/to/kb   # or a local build

**Default to S3 on purpose.** The index must describe the corpus the knowledge
base actually holds, not the one on this laptop. Measured 2026-08-21 they
differ by 75 chunks — ``../health.studio/build/kb`` carries 379 graph chunks
against the deployed 304, almost all of it the nerve registry (52 local vs 8
ingested). Indexing the local tree would mint edges to chunks ``Retrieve``
cannot return: expansion would look richer and retrieve less, which is the
worst possible failure for a measurement. Build from a local path only when
you have just synced it.

The index is a build artifact, not source — it is regenerated from the corpus
and gitignored, exactly like ``status.json``. Rebuild it whenever the corpus
is re-synced, or expansion will traverse edges the KB no longer holds.

Prints a coverage report because the parser fails **silently** when the corpus
shifts: a lowercase-only id pattern once parsed zero ``in_chain`` edges across
all 62 files that carry them, and nothing errored. Watch ``edges_by_relation``
for a relation that drops to zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from docpipe_core.graph import build_graph, build_report

_DEFAULT_OUT = Path(__file__).resolve().parents[1] / ".scratch" / "graph-index.json"


def read_corpus_s3(uri: str) -> dict[str, str]:
    """``{chunk_key: markdown}`` for graph chunks under an ``s3://`` prefix.

    Keys are made relative to the prefix so they match the local reader and,
    through it, ``kb_eval.chunk_key_of``.
    """
    import boto3

    parsed = urlparse(uri)
    bucket, prefix = parsed.netloc, parsed.path.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    client = boto3.client("s3")
    documents: dict[str, str] = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = key.rsplit("/", 1)[-1]
            if not name.startswith("body-graph-") or not name.endswith(".md"):
                continue
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
            documents[key[len(prefix) :]] = body.decode("utf-8")
    return documents


def read_corpus(source: Path) -> dict[str, str]:
    """``{chunk_key: markdown}`` for every graph chunk under ``source``.

    Keys are corpus-relative (``graph/body-graph-connection--abdominals.md``)
    so they line up with what ``kb_eval.chunk_key_of`` derives from a retrieved
    passage's S3 URI — that correspondence is the whole join between retrieval
    and the graph, so it must not drift.
    """
    documents: dict[str, str] = {}
    for path in sorted(source.rglob("*.md")):
        if path.name.endswith(".metadata.json") or not path.name.startswith("body-graph-"):
            continue
        documents[str(path.relative_to(source))] = path.read_text(encoding="utf-8")
    return documents


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="local dir or s3://bucket/prefix")
    parser.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = parser.parse_args(argv)

    if str(args.source).startswith("s3://"):
        documents = read_corpus_s3(str(args.source))
    else:
        source = Path(args.source)
        if not source.is_dir():
            print(f"no corpus at {source} — pass --source", file=sys.stderr)
            return 2
        documents = read_corpus(source)
        print(
            "NOTE: indexing a LOCAL corpus. If it is ahead of what has been "
            "ingested, expansion will hop to chunks Retrieve cannot return.",
            file=sys.stderr,
        )
    if not documents:
        print(f"no body-graph-*.md under {args.source}", file=sys.stderr)
        return 2

    graph = build_graph(documents)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(graph.to_json(), encoding="utf-8")

    report = build_report(documents, graph)
    print(json.dumps(report, indent=2))
    print(f"\n{args.out}  ({args.out.stat().st_size / 1024:.1f} KB)")

    empty = [
        rel
        for rel in ("in_chain", "has_member", "innervated_by", "supplies")
        if not report["edges_by_relation"].get(rel)
    ]
    if empty:
        print(
            f"\nWARNING: no edges parsed for {', '.join(empty)} — parser may have drifted",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
