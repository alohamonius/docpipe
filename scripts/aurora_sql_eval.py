"""The two exhibits only Aurora can produce, over the RDS Data API.

    AWS_PROFILE=docpipe uv run --with boto3 python scripts/aurora_sql_eval.py \
        --questions ../health.studio/.../questions.json --out .scratch/sweep.json
    AWS_PROFILE=docpipe uv run --with boto3 python scripts/aurora_sql_eval.py \
        --near-dupes --out .scratch/near-dupes.json

**This is a different instrument than the Retrieve path and must be labeled as
such in any chart.** Bedrock issues its own SQL sessions, so `hnsw.ef_search`
— a session-level knob — can never be set on the shipped path; the sweep
therefore talks to Postgres directly. Consequences the reader of its numbers
needs: query embedding happens here (same Titan v2 @ 1024 both KBs pin, cosine
is scale-invariant so normalization cannot change ranks), latency is the Data
API round trip (HTTP, not in-VPC SQL), and there is no score-inversion issue —
`<=>` is cosine distance read directly, ranked ascending, no Bedrock in the
middle (FINDINGS 2026-08-20).

Default sweep: ef_search 10/40/100/200 (pgvector's default is 40). Each value
runs inside one transaction — `SET LOCAL` lives exactly that long — with the
top-10 ranked list and per-query wall time recorded, so recall@4/5/10 and MRR
come out of a single pass.

`--near-dupes` instead self-joins the 585 embeddings (`<=>` < threshold) and
answers "are my chunks actually distinct?" — a question the Retrieve API
cannot express.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from docpipe_core.kb_eval import UnratifiedAnswerKey, load_question_set
from docpipe_core.kb_sync import DEFAULT_PREFIX

PROFILE = "docpipe"
REGION = "us-east-1"
EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"  # pinned in both KB components
EMBEDDING_DIMENSION = 1024
TABLE = "bedrock_integration.bedrock_kb"
TOP_N = 10  # one ranked list serves recall@4/5/10 and MRR


def stack_outputs() -> dict[str, str]:
    raw = subprocess.run(
        ["pulumi", "stack", "output", "--json", "--stack", "dev"],
        cwd="infra",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(raw)


def key_of_uri(uri: str | None, prefix: str = DEFAULT_PREFIX) -> str | None:
    """Same contract as ``chunk_key_of``: s3://…/corpus/a/b.md → a/b.md."""
    if not uri:
        return None
    marker = f"/{prefix}" if not prefix.startswith("/") else prefix
    at = uri.find(marker)
    if at == -1:
        return None
    return uri[at + len(marker) :].lstrip("/") or None


class DataApi:
    """Thin Data API wrapper: auto-pause retry, optional transaction scope."""

    def __init__(self, client: Any, cluster_arn: str, secret_arn: str, database: str) -> None:
        self._client = client
        self._cluster_arn = cluster_arn
        self._secret_arn = secret_arn
        self._database = database

    def _common(self) -> dict[str, str]:
        return {
            "resourceArn": self._cluster_arn,
            "secretArn": self._secret_arn,
            "database": self._database,
        }

    def execute(self, sql: str, transaction_id: str | None = None) -> dict[str, Any]:
        kwargs = self._common() | {"sql": sql}
        if transaction_id:
            kwargs["transactionId"] = transaction_id
        for attempt in range(3):
            try:
                return self._client.execute_statement(**kwargs)
            except ClientError as e:
                code = e.response["Error"]["Code"]
                # min_capacity=0 resume — documented ~15s, only this is retried.
                if code in ("DatabaseResumingException", "BadRequestException") and attempt < 2:
                    wait = 10 * (attempt + 1)
                    print(f"    cluster resuming ({code}) — waiting {wait}s", flush=True)
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError("unreachable")

    def begin(self) -> str:
        return self._client.begin_transaction(**self._common())["transactionId"]

    def commit(self, transaction_id: str) -> None:
        self._client.commit_transaction(
            resourceArn=self._cluster_arn,
            secretArn=self._secret_arn,
            transactionId=transaction_id,
        )


def embed(bedrock_runtime: Any, text: str) -> list[float]:
    body = json.dumps({"inputText": text, "dimensions": EMBEDDING_DIMENSION, "normalize": True})
    response = bedrock_runtime.invoke_model(modelId=EMBEDDING_MODEL_ID, body=body)
    return json.loads(response["body"].read())["embedding"]


def vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.8g}" for x in vec) + "]"


def ranked_query(db: DataApi, vec: list[float], transaction_id: str) -> tuple[list[str], float]:
    """Top-N chunk keys by cosine distance, plus the call's wall seconds."""
    sql = (
        f"SELECT metadata->>'sourceUrl' FROM {TABLE} "
        f"ORDER BY embedding <=> '{vector_literal(vec)}'::vector LIMIT {TOP_N}"
    )
    started = time.perf_counter()
    response = db.execute(sql, transaction_id)
    wall = time.perf_counter() - started
    keys = [
        key
        for row in response["records"]
        if (key := key_of_uri(row[0].get("stringValue"))) is not None
    ]
    return keys, wall


def run_sweep(db: DataApi, bedrock_runtime: Any, args: argparse.Namespace) -> dict[str, Any]:
    question_set = load_question_set(args.questions)
    if not args.dry_run and not question_set.is_ratified:
        raise UnratifiedAnswerKey("unratified key; pass --dry-run for a rehearsal")
    questions = question_set.questions[: args.limit] if args.limit else question_set.questions

    print(f"embedding {len(questions)} questions ({EMBEDDING_MODEL_ID})", flush=True)
    vectors = {q.id: embed(bedrock_runtime, q.question) for q in questions}

    # Pay the auto-pause resume before any timed call.
    db.execute(f"SELECT count(*) FROM {TABLE}")

    curve: list[dict[str, Any]] = []
    for ef in args.ef_search:
        transaction_id = db.begin()
        db.execute(f"SET LOCAL hnsw.ef_search = {ef}", transaction_id)
        per_question: list[dict[str, Any]] = []
        for i, q in enumerate(questions, 1):
            returned, wall = ranked_query(db, vectors[q.id], transaction_id)
            hit_ranks = [pos for pos, key in enumerate(returned, 1) if key in q.expected]
            per_question.append(
                {
                    "id": q.id,
                    "question_class": q.question_class,
                    "rank": hit_ranks[0] if hit_ranks else None,
                    "forbidden_returned": sorted(set(returned) & set(q.must_not_return)),
                    "wall_s": round(wall, 4),
                    "returned": returned,
                }
            )
            rank = per_question[-1]["rank"]
            print(f"  ef={ef} [{i}/{len(questions)}] {q.id} rank={rank}", flush=True)
        db.commit(transaction_id)

        scored = [r for r in per_question if r["question_class"] != "not-covered"]
        walls = sorted(r["wall_s"] for r in per_question)

        def recall_at(k: int, rows: list[dict[str, Any]] = scored) -> float:
            return round(sum(1 for r in rows if r["rank"] and r["rank"] <= k) / len(rows), 4)

        curve.append(
            {
                "ef_search": ef,
                "recall_at_4": recall_at(4),
                "recall_at_5": recall_at(5),
                "recall_at_10": recall_at(TOP_N),
                "mrr_at_10": round(
                    sum(1 / r["rank"] for r in scored if r["rank"]) / len(scored), 4
                ),
                "latency_p50_s": walls[len(walls) // 2],
                "latency_p95_s": walls[min(len(walls) - 1, int(0.95 * len(walls)))],
                "forbidden_violations": [r["id"] for r in per_question if r["forbidden_returned"]],
                "results": per_question,
            }
        )

    return {
        "mode": "ef_search_sweep",
        "instrument": "direct SQL via RDS Data API — NOT the Bedrock Retrieve path",
        "embedding_model_id": EMBEDDING_MODEL_ID,
        "dimension": EMBEDDING_DIMENSION,
        "n": len(questions),
        "top_n": TOP_N,
        "question_set": {
            "version": question_set.version,
            "authored": question_set.authored,
            "ratified": question_set.ratified,
            "ratified_by": question_set.ratified_by,
        },
        "curve": curve,
    }


def run_near_dupes(db: DataApi, args: argparse.Namespace) -> dict[str, Any]:
    db.execute(f"SELECT count(*) FROM {TABLE}")  # resume before timing anything
    total = db.execute(f"SELECT count(*) FROM {TABLE}")["records"][0][0]["longValue"]
    count_sql = (
        f"SELECT count(*) FROM {TABLE} a JOIN {TABLE} b ON a.id < b.id "
        f"WHERE a.embedding <=> b.embedding < {args.threshold}"
    )
    pair_count = db.execute(count_sql)["records"][0][0]["longValue"]
    pairs_sql = (
        f"SELECT a.metadata->>'sourceUrl', b.metadata->>'sourceUrl', "
        f"round((a.embedding <=> b.embedding)::numeric, 4) "
        f"FROM {TABLE} a JOIN {TABLE} b ON a.id < b.id "
        f"WHERE a.embedding <=> b.embedding < {args.threshold} "
        f"ORDER BY a.embedding <=> b.embedding LIMIT {args.max_pairs}"
    )
    rows = db.execute(pairs_sql)["records"]
    pairs = [
        {
            "a": key_of_uri(row[0].get("stringValue")),
            "b": key_of_uri(row[1].get("stringValue")),
            "cosine_distance": float(row[2]["stringValue"]),
        }
        for row in rows
    ]
    return {
        "mode": "near_dupes",
        "instrument": "direct SQL via RDS Data API — NOT the Bedrock Retrieve path",
        "chunks": total,
        "threshold": args.threshold,
        "pair_count": pair_count,
        "pairs_listed": len(pairs),
        "pairs": pairs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--ef-search",
        type=int,
        nargs="+",
        default=[10, 40, 100, 200],
        help="values to sweep; pgvector's default is 40",
    )
    parser.add_argument("--near-dupes", action="store_true")
    parser.add_argument("--threshold", type=float, default=0.15)
    parser.add_argument("--max-pairs", type=int, default=100)
    parser.add_argument("--limit", type=int, default=None, help="first N questions (smoke test)")
    parser.add_argument("--dry-run", action="store_true", help="allow an unratified key")
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--region", default=REGION)
    args = parser.parse_args()
    if not args.near_dupes and not args.questions:
        parser.error("--questions is required unless --near-dupes")

    out = stack_outputs()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    db = DataApi(
        session.client("rds-data"),
        cluster_arn=out["aurora_cluster_arn"],
        secret_arn=out["aurora_bedrock_secret_arn"],
        database=out["aurora_database_name"],
    )

    if args.near_dupes:
        report = run_near_dupes(db, args)
    else:
        report = run_sweep(db, session.client("bedrock-runtime"), args)

    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        print(f"report → {args.out}")
    summary = {
        k: ([{x: c[x] for x in c if x != "results"} for c in v] if k == "curve" else v)
        for k, v in report.items()
        if k != "pairs"
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
