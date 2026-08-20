"""Score the live knowledge base against health.studio's held-out question set.

    make kb-eval QUESTIONS=../health.studio/apps/web/src/lib/kb/eval/questions.json \
                 KB_ID=SJQAFQXPH7 OUT=.scratch/baseline.json

A thin shell on purpose. Every decision — how a passage maps back to a chunk
key, what counts as a complete evidence stamp, how recall/MRR/abstention are
computed — lives in ``docpipe_core.kb_eval``, which is typechecked and has 23
unit tests. `services/` and `scripts/` have neither, and a metric nobody can
test is a metric nobody should trust.

It will refuse to run against an unratified answer key. That is not a
formality: an agent that writes both the questions and the correct answers has
graded its own homework, and a score taken against a self-graded key is a
number wearing the word "baseline". Pass ``--dry-run`` to exercise the harness
anyway — the report then carries `ratified: null`, which is how a reader knows
what they are looking at.

Costs real Bedrock retrieval calls: one per question, 63 today.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3
from docpipe_core.kb_eval import (
    UnratifiedAnswerKey,
    load_question_set,
    score_question_set,
)
from docpipe_core.retrieval import DEFAULT_RERANK_POOL, KnowledgeBaseClient

PROFILE = "docpipe"
REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--min-evidence",
        type=int,
        default=None,
        help="evidence floor for the shipped filter; omit to measure the corpus unfiltered",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="score an unratified answer key — a rehearsal, never a baseline",
    )
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="widen retrieval to a candidate pool and rerank with cohere.rerank-v3-5 "
        "before cutting to k; the report records this so the number cannot be "
        "mistaken for a raw-retrieval score",
    )
    parser.add_argument(
        "--rerank-pool",
        type=int,
        default=DEFAULT_RERANK_POOL,
        help="candidate pool width when --rerank is set "
        "(default sized by the 2026-08-16 miss-rank probe)",
    )
    parser.add_argument(
        "--search-type",
        choices=("SEMANTIC", "HYBRID"),
        default=None,
        help="pin overrideSearchType; omit to let the store decide (the pre-Aurora "
        "baselines were all taken with it omitted)",
    )
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--region", default=REGION)
    args = parser.parse_args()

    question_set = load_question_set(args.questions)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    runtime = session.client("bedrock-agent-runtime")
    client = KnowledgeBaseClient(args.kb_id, agent_runtime_client=runtime)

    try:
        report = score_question_set(
            client,
            question_set,
            k=args.k,
            min_evidence=args.min_evidence,
            rerank=args.rerank,
            rerank_pool=args.rerank_pool,
            search_type=args.search_type,
            require_ratified=not args.dry_run,
        )
    except UnratifiedAnswerKey as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return 2

    rendered = report.model_dump_json(indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        print(f"report → {args.out}")
    else:
        print(rendered)

    overall = report.overall
    mode = f"rerank(pool={report.rerank_pool})" if report.rerank else "raw"
    if report.search_type:
        mode += f"·{report.search_type}"
    print(
        f"n={report.n} k={report.k} [{mode}] · recall@k {overall.recall_at_k} · MRR {overall.mrr} "
        f"· stamp integrity {overall.stamp_integrity} ({overall.stamped}/{overall.passages})"
    )
    for stratum in report.by_stratum:
        line = (
            f"  {stratum.stratum:<22} n={stratum.n:<3} recall {stratum.recall_at_k:<6} "
            f"MRR {stratum.mrr:<6} stamp {stratum.stamp_integrity}"
        )
        if stratum.abstention is not None:
            line += f" abstention {stratum.abstention}"
        print(line)
    if report.forbidden_violations:
        print(f"CONFABULATION: {', '.join(report.forbidden_violations)}", file=sys.stderr)

    # A forbidden chunk coming back is a safety result, not a scoring detail:
    # on gap-04 it is the system answering a question about chest pain with a
    # gallbladder red flag. Non-zero exit so a scheduled re-run cannot pass it
    # by printing a number and returning 0.
    return 1 if report.forbidden_violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
