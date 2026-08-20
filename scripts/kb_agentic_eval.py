"""Score *agentic* retrieval: the model drives search_kb, we grade the session.

    make kb-agentic-eval KB_ID=... OUT=... [SEARCH="--search-type HYBRID"] \
        [FLAGS="--semantic-score-is-distance"]

A thin shell, same contract as ``kb_eval.py``: the loop itself lives in
``docpipe_core.agent`` (typed, 8 unit tests), scoring reuses the tested
``chunk_key_of``, and this file only parses arguments, iterates questions and
writes the report.

**The metrics are session-level and deliberately NOT MRR.** A multi-query
session has no single ranked list, so reporting MRR here would invite
comparison with the single-shot reports as if they measured the same thing.
What is scored instead, per question:

* ``session_hit``   — an expected chunk appeared in ANY tool call's top-k
* ``first_hit_call``/``rank_at_first_hit`` — how much work it took
* ``forbidden_returned`` — a must-not-return chunk appeared in ANY call
* ``tool_calls``, tokens, wall seconds — the stateless tax, per question
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

import boto3
from docpipe_core.agent import DEFAULT_MAX_ITERATIONS, KbAgent
from docpipe_core.kb_eval import UnratifiedAnswerKey, chunk_key_of, load_question_set
from docpipe_core.retrieval import KnowledgeBaseClient

PROFILE = "docpipe"
REGION = "us-east-1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--kb-id", required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--search-type", choices=("SEMANTIC", "HYBRID"), default=None)
    parser.add_argument("--semantic-score-is-distance", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="first N questions (smoke test)")
    parser.add_argument("--dry-run", action="store_true", help="allow an unratified key")
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--region", default=REGION)
    args = parser.parse_args()

    question_set = load_question_set(args.questions)
    if not args.dry_run and not question_set.is_ratified:
        raise UnratifiedAnswerKey("unratified key; pass --dry-run for a rehearsal")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    kb = KnowledgeBaseClient(
        args.kb_id,
        agent_runtime_client=session.client("bedrock-agent-runtime"),
        semantic_score_is_distance=args.semantic_score_is_distance,
    )
    agent = KbAgent(
        kb,
        bedrock_client=session.client("bedrock-runtime"),
        top_k=args.k,
        search_type=args.search_type,
    )

    questions = question_set.questions[: args.limit] if args.limit else question_set.questions
    results: list[dict[str, Any]] = []
    for i, q in enumerate(questions, 1):
        run = agent.ask(q.question)
        retrievals = [
            [key for p in call.passages if (key := chunk_key_of(p)) is not None]
            for call in run.tool_calls
        ]
        union = [key for call in retrievals for key in call]
        first_hit_call = rank_at_first_hit = None
        for call_no, call in enumerate(retrievals, 1):
            hit_ranks = [pos for pos, key in enumerate(call, 1) if key in q.expected]
            if hit_ranks:
                first_hit_call, rank_at_first_hit = call_no, hit_ranks[0]
                break
        results.append(
            {
                "id": q.id,
                "stratum": q.stratum,
                "question_class": q.question_class,
                "session_hit": first_hit_call is not None,
                "first_hit_call": first_hit_call,
                "rank_at_first_hit": rank_at_first_hit,
                "tool_calls": len(retrievals),
                "forbidden_returned": sorted({k_ for k_ in union if k_ in q.must_not_return}),
                "stop_reason": run.stop_reason,
                "tokens_in": run.input_tokens,
                "tokens_out": run.output_tokens,
                "wall_s": run.wall_s,
                "queries_retrieved": retrievals,
            }
        )
        print(
            f"[{i}/{len(questions)}] {q.id}: hit={first_hit_call is not None} "
            f"calls={len(retrievals)} wall={run.wall_s}s",
            flush=True,
        )

    scored = [r for r in results if r["question_class"] != "not-covered"]
    gap = [r for r in results if r["question_class"] == "not-covered"]
    walls = sorted(r["wall_s"] for r in results)
    report = {
        "mode": "agentic",
        "model_id": agent.model_id,
        "max_iterations": DEFAULT_MAX_ITERATIONS,
        "k": args.k,
        "n": len(results),
        "search_type": args.search_type,
        "semantic_score_is_distance": args.semantic_score_is_distance,
        "rerank": False,
        "knowledge_base_id": args.kb_id,
        "question_set": {
            "version": question_set.version,
            "authored": question_set.authored,
            "ratified": question_set.ratified,
            "ratified_by": question_set.ratified_by,
        },
        "session_recall": round(sum(r["session_hit"] for r in scored) / len(scored), 4),
        "abstention": (
            round(sum(1 for r in gap if not r["forbidden_returned"]) / len(gap), 4) if gap else None
        ),
        "forbidden_violations": [r["id"] for r in results if r["forbidden_returned"]],
        "no_tool_questions": [r["id"] for r in results if r["tool_calls"] == 0],
        "mean_tool_calls": round(statistics.mean(r["tool_calls"] for r in results), 2),
        "tokens_in_total": sum(r["tokens_in"] for r in results),
        "tokens_out_total": sum(r["tokens_out"] for r in results),
        "wall_p50_s": walls[len(walls) // 2],
        "wall_p95_s": walls[min(len(walls) - 1, int(0.95 * len(walls)))],
        "results": results,
    }
    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        print(f"report → {args.out}")
    summary = {k_: v for k_, v in report.items() if k_ != "results"}
    print(json.dumps(summary, indent=2))
    return 1 if report["forbidden_violations"] else 0


if __name__ == "__main__":
    sys.exit(main())
