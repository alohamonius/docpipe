"""Probe a Bedrock guardrail with ApplyGuardrail: which policy fires, on what, how often.

    uv run --with boto3 python scripts/guardrail_probe.py \
        --questions .../questions.json --guardrail-id ... [--repeats 5] [--out ...]
    uv run --with boto3 python scripts/guardrail_probe.py \
        --guardrail-id ... --text "my neck clicks when I turn my head" [--source OUTPUT]

Exists because the agentic eval can only observe ``stop_reason ==
"guardrail_intervened"`` — Converse replaces the response with the canned
blocked message and (without trace) says nothing about WHICH policy matched.
ApplyGuardrail returns the full assessment: denied-topic hits, content-filter
hits with confidence, PII actions. The 2026-08-23 re-baseline showed blocking
is stochastic (31/61 ever-blocked, zero always-blocked), so a single pass per
question measures nothing — ``--repeats`` runs each text N times and reports a
block rate, which is the number tuning has to drive to zero.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import boto3
from docpipe_core.kb_eval import load_question_set

PROFILE = "docpipe"
REGION = "us-east-1"


def fired_policies(assessments: list[dict[str, Any]]) -> list[str]:
    """Flatten an ApplyGuardrail assessment list into 'kind:name' strings.

    Only entries whose action is a block/anonymize count — the assessment can
    mention policies that evaluated and passed.
    """
    fired: list[str] = []
    for a in assessments:
        for topic in a.get("topicPolicy", {}).get("topics", []):
            if topic.get("action") == "BLOCKED":
                fired.append(f"topic:{topic['name']}")
        for f in a.get("contentPolicy", {}).get("filters", []):
            if f.get("action") == "BLOCKED":
                fired.append(f"filter:{f['type']}:{f.get('confidence', '?')}")
        for w in a.get("wordPolicy", {}).get("customWords", []):
            if w.get("action") == "BLOCKED":
                fired.append(f"word:{w.get('match', '?')}")
        for p in a.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
            if p.get("action") in ("BLOCKED", "ANONYMIZED"):
                fired.append(f"pii:{p['type']}:{p['action']}")
    return fired


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guardrail-id", required=True)
    parser.add_argument("--guardrail-version", default="DRAFT")
    parser.add_argument("--questions", type=Path, default=None)
    parser.add_argument("--text", default=None, help="ad-hoc probe instead of a question set")
    parser.add_argument("--source", choices=("INPUT", "OUTPUT"), default="INPUT")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None, help="first N questions (smoke test)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--region", default=REGION)
    args = parser.parse_args()
    if (args.questions is None) == (args.text is None):
        parser.error("exactly one of --questions / --text")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    runtime = session.client("bedrock-runtime")

    if args.text is not None:
        probes: list[dict[str, Any]] = [{"id": "adhoc", "text": args.text}]
    else:
        qs = load_question_set(args.questions).questions
        if args.limit:
            qs = qs[: args.limit]
        probes = [
            {"id": q.id, "stratum": q.stratum, "question_class": q.question_class,
             "text": q.question}
            for q in qs
        ]

    def assess_once(text: str) -> tuple[bool, list[str]]:
        resp = runtime.apply_guardrail(
            guardrailIdentifier=args.guardrail_id,
            guardrailVersion=args.guardrail_version,
            source=args.source,
            content=[{"text": {"text": text}}],
        )
        blocked = resp["action"] == "GUARDRAIL_INTERVENED"
        return blocked, fired_policies(resp.get("assessments", []))

    def probe_one(numbered: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        i, p = numbered
        blocked_n = 0
        policies: Counter[str] = Counter()
        for _ in range(args.repeats):
            blocked, fired = assess_once(p["text"])
            blocked_n += blocked
            policies.update(fired)
        print(
            f"[{i}/{len(probes)}] {p['id']}: blocked {blocked_n}/{args.repeats}"
            + (f" via {dict(policies)}" if policies else ""),
            flush=True,
        )
        return {**p, "blocked": blocked_n, "repeats": args.repeats, "policies": dict(policies)}

    numbered = list(enumerate(probes, 1))
    if args.workers > 1 and len(probes) > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            results = list(pool.map(probe_one, numbered))
    else:
        results = [probe_one(pair) for pair in numbered]

    policy_totals: Counter[str] = Counter()
    for r in results:
        policy_totals.update(r["policies"])
    report = {
        "mode": "guardrail_probe",
        "guardrail_id": args.guardrail_id,
        "guardrail_version": args.guardrail_version,
        "source": args.source,
        "repeats": args.repeats,
        "n": len(results),
        "always_blocked": [r["id"] for r in results if r["blocked"] == r["repeats"]],
        "sometimes_blocked": [r["id"] for r in results if 0 < r["blocked"] < r["repeats"]],
        "never_blocked_n": sum(1 for r in results if r["blocked"] == 0),
        "block_rate_overall": round(
            sum(r["blocked"] for r in results) / (len(results) * args.repeats), 4
        ),
        "policy_totals": dict(policy_totals.most_common()),
        "results": results,
    }
    rendered = json.dumps(report, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
        print(f"report → {args.out}")
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
