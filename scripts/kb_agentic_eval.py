"""Score *agentic* retrieval: the model drives search_kb, we grade the session.

    make kb-agentic-eval KB_ID=... OUT=... [SEARCH="--search-type HYBRID"] \
        [FLAGS="--semantic-score-is-distance"]

The single-shot harness (`kb_eval.py`) measures one Retrieve per question.
This one measures the loop the agent actually runs (FINDINGS 2026-08-18): a
hand-written Converse tool loop, `deepseek.v3.2`, one `search_kb(query,
min_evidence)` tool, the model free to reformulate and call it repeatedly up
to MAX_ITERATIONS.

**The metrics are session-level and deliberately NOT MRR.** A multi-query
session has no single ranked list, so reporting MRR here would invite
comparison with the single-shot reports as if they measured the same thing.
What is scored instead, per question:

* ``session_hit``   — an expected chunk appeared in ANY tool call's top-k
* ``first_hit_call``/``rank_at_first_hit`` — how much work it took
* ``forbidden_returned`` — a must-not-return chunk appeared in ANY call
* ``tool_calls``, tokens, wall seconds — the stateless tax, per question

Rerank stays OFF inside the tool: cohere.rerank-v3-5 is 3 req/min at ACCOUNT
level, and an agent making 3 tool calls per question would starve on it.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError
from docpipe_core.kb_eval import UnratifiedAnswerKey, chunk_key_of, load_question_set
from docpipe_core.llm import HEALTH_ASSISTANT_SYSTEM
from docpipe_core.retrieval import KnowledgeBaseClient

PROFILE = "docpipe"
REGION = "us-east-1"
MODEL_ID = "deepseek.v3.2"  # ON_DEMAND, tool use; R1 has neither (FINDINGS 2026-08)
MAX_ITERATIONS = 5

TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": "search_kb",
                "description": (
                    "Search the health.studio knowledge base for evidence-graded "
                    "passages about anatomy, pain, training and recovery. Pass "
                    "min_evidence=1 when the question is about the user's pain."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "min_evidence": {"type": "integer", "minimum": 0, "maximum": 3},
                        },
                        "required": ["query"],
                    }
                },
            }
        }
    ]
}


def _with_backoff(call, *, retries: int = 4):
    """Throttle-tolerant wrapper; jittered exponential, same shape as llm.py."""
    for attempt in range(retries + 1):
        try:
            return call()
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code not in ("ThrottlingException", "ServiceUnavailableException") or (
                attempt == retries
            ):
                raise
            time.sleep(2**attempt + random.random())
    raise RuntimeError("unreachable")


def run_session(
    runtime: Any,
    kb: KnowledgeBaseClient,
    question: str,
    *,
    k: int,
    search_type: str | None,
) -> dict[str, Any]:
    """One question through the tool loop; returns raw per-session telemetry."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": [{"text": question}]}]
    retrievals: list[list[str]] = []
    tokens_in = tokens_out = 0
    stop = None
    t0 = time.perf_counter()

    for _ in range(MAX_ITERATIONS):
        response = _with_backoff(
            lambda: runtime.converse(
                modelId=MODEL_ID,
                system=[{"text": HEALTH_ASSISTANT_SYSTEM + " Use the search_kb tool for facts."}],
                messages=messages,
                toolConfig=TOOL_CONFIG,
                inferenceConfig={"maxTokens": 1024, "temperature": 0.3},
            )
        )
        usage = response.get("usage", {})
        tokens_in += usage.get("inputTokens", 0)
        tokens_out += usage.get("outputTokens", 0)
        stop = response.get("stopReason")
        output_message = response["output"]["message"]
        if stop != "tool_use":
            break

        messages.append({"role": "assistant", "content": output_message["content"]})
        for block in output_message["content"]:
            if "toolUse" not in block:
                continue
            tool_use = block["toolUse"]
            query = tool_use["input"]["query"]
            floor = tool_use["input"].get("min_evidence")
            passages = _with_backoff(
                lambda query=query, floor=floor: kb.retrieve(
                    query,
                    top_k=k,
                    min_evidence=floor,
                    rerank=False,
                    search_type=search_type,
                )
            )
            retrievals.append([key for p in passages if (key := chunk_key_of(p)) is not None])
            rendered = (
                "\n\n".join(f"[{p.citation}]\n{p.text[:600]}" for p in passages)
                or "No passages found."
            )
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "toolResult": {
                                "toolUseId": tool_use["toolUseId"],
                                "content": [{"text": rendered}],
                            }
                        }
                    ],
                }
            )

    return {
        "retrievals": retrievals,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "stop_reason": stop,
        "wall_s": round(time.perf_counter() - t0, 2),
    }


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
    runtime = session.client("bedrock-runtime")
    kb = KnowledgeBaseClient(
        args.kb_id,
        agent_runtime_client=session.client("bedrock-agent-runtime"),
        semantic_score_is_distance=args.semantic_score_is_distance,
    )

    questions = question_set.questions[: args.limit] if args.limit else question_set.questions
    results = []
    for i, q in enumerate(questions, 1):
        raw = run_session(runtime, kb, q.question, k=args.k, search_type=args.search_type)
        union = [key for call in raw["retrievals"] for key in call]
        first_hit_call = rank_at_first_hit = None
        for call_no, call in enumerate(raw["retrievals"], 1):
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
                "tool_calls": len(raw["retrievals"]),
                "forbidden_returned": sorted({k_ for k_ in union if k_ in q.must_not_return}),
                "stop_reason": raw["stop_reason"],
                "tokens_in": raw["tokens_in"],
                "tokens_out": raw["tokens_out"],
                "wall_s": raw["wall_s"],
                "queries_retrieved": raw["retrievals"],
            }
        )
        print(
            f"[{i}/{len(questions)}] {q.id}: hit={first_hit_call is not None} "
            f"calls={len(raw['retrievals'])} wall={raw['wall_s']}s",
            flush=True,
        )

    n = len(results)
    scored = [r for r in results if r["question_class"] != "not-covered"]
    gap = [r for r in results if r["question_class"] == "not-covered"]
    walls = sorted(r["wall_s"] for r in results)
    report = {
        "mode": "agentic",
        "model_id": MODEL_ID,
        "max_iterations": MAX_ITERATIONS,
        "k": args.k,
        "n": n,
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
