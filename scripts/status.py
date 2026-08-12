"""Gather live docpipe status → ``status.json`` → render ``dashboard.html``.

Every number on the dashboard comes from a real API call or a file in this repo;
nothing is hardcoded in the template. Re-run with ``make status``.

Each probe is independent and failure-tolerant: a missing permission or a
not-yet-deployed service shows up as a degraded card, not a crash.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

REPO = Path(__file__).resolve().parents[1]
PROFILE = "docpipe"
REGION = "us-east-1"

session = boto3.Session(profile_name=PROFILE, region_name=REGION)

# ---------------------------------------------------------------- helpers


def probe(fn, *args, **kwargs) -> tuple[Any, str | None]:
    """Run a probe; return (value, error). Never raises."""
    try:
        return fn(*args, **kwargs), None
    except (
        ClientError,
        BotoCoreError,
        KeyError,
        IndexError,
        StopIteration,
        OSError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as exc:
        return None, str(exc).split(":")[-1].strip()[:120]


def mask(text: str | None) -> str:
    """Hide the AWS account id in resource names — the dashboard gets shared."""
    return re.sub(r"\d{12}", lambda m: "…" + m.group()[-4:], text or "")


# ---------------------------------------------------------------- probes


def _pulumi(*argv: str) -> Any:
    """Run a pulumi command in the dev stack and parse its JSON output."""
    env = {**os.environ, "PATH": os.environ["PATH"] + ":" + str(Path.home() / ".pulumi/bin")}
    env.setdefault("PULUMI_CONFIG_PASSPHRASE", "")
    out = subprocess.run(
        ["pulumi", *argv, "--cwd", str(REPO / "pulumi")],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return json.loads(out.stdout)


def pulumi_stack() -> dict:
    """Resource inventory from the Pulumi state, grouped by provider type."""
    resources = _pulumi("stack", "export")["deployment"]["resources"]
    kinds: dict[str, int] = {}
    for r in resources:
        t = r["type"]
        if t.startswith("pulumi:") or t.startswith("aws:providers"):
            continue
        # aws:ec2/vpc:Vpc → ec2 ; pulumi-python:dynamic:Resource → dynamic
        svc = t.split(":")[1].split("/")[0] if ":" in t else t
        kinds[svc] = kinds.get(svc, 0) + 1
    return {
        "total": sum(kinds.values()),
        "by_service": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
    }


def stack_outputs() -> dict:
    return _pulumi("stack", "output", "--json")


def dynamo(table: str) -> dict:
    t = session.client("dynamodb").describe_table(TableName=table)["Table"]
    return {
        "status": t["TableStatus"],
        "items": t.get("ItemCount", 0),
        "size_bytes": t.get("TableSizeBytes", 0),
        "billing": t.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED"),
        "keys": "/".join(k["AttributeName"] for k in t["KeySchema"]),
    }


def sqs(url: str) -> dict:
    attrs = session.client("sqs").get_queue_attributes(
        QueueUrl=url,
        AttributeNames=[
            "ApproximateNumberOfMessages",
            "ApproximateNumberOfMessagesNotVisible",
            "RedrivePolicy",
        ],
    )["Attributes"]
    return {
        "visible": int(attrs.get("ApproximateNumberOfMessages", 0)),
        "in_flight": int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)),
        "dlq": "RedrivePolicy" in attrs,
    }


def s3_bucket(name: str) -> dict:
    c = session.client("s3")
    page = c.list_objects_v2(Bucket=name, MaxKeys=1000)
    ver = c.get_bucket_versioning(Bucket=name).get("Status", "Disabled")
    return {
        "objects": page.get("KeyCount", 0),
        "bytes": sum(o["Size"] for o in page.get("Contents", [])),
        "versioning": ver,
        "truncated": page.get("IsTruncated", False),
    }


def knowledge_base(kb_id: str, ds_id: str) -> dict:
    c = session.client("bedrock-agent")
    kb = c.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]
    cfg = kb["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    jobs = c.list_ingestion_jobs(knowledgeBaseId=kb_id, dataSourceId=ds_id, maxResults=5)
    js = jobs.get("ingestionJobSummaries", [])
    last = js[0] if js else None
    return {
        "status": kb["status"],
        "embedding_model": cfg["embeddingModelArn"].split("/")[-1],
        "store": kb["storageConfiguration"]["type"],
        "ingestion_jobs": len(js),
        "last_job": {
            "status": last["status"],
            "indexed": last.get("statistics", {}).get("numberOfDocumentsScanned", 0),
        }
        if last
        else None,
    }


def guardrail(gid: str) -> dict:
    g = session.client("bedrock").get_guardrail(guardrailIdentifier=gid, guardrailVersion="DRAFT")
    return {
        "status": g["status"],
        "denied_topics": len(g.get("topicPolicy", {}).get("topics", [])),
        "content_filters": len(g.get("contentPolicy", {}).get("filters", [])),
        "pii_entities": len(g.get("sensitiveInformationPolicy", {}).get("piiEntities", [])),
    }


def vectors(bucket: str, index: str) -> dict:
    c = session.client("s3vectors")
    ix = c.get_index(vectorBucketName=bucket, indexName=index)["index"]
    return {
        "dimension": ix["dimension"],
        "metric": ix["distanceMetric"],
        "data_type": ix["dataType"],
    }


def invocation_logging() -> dict:
    cfg = session.client("bedrock").get_model_invocation_logging_configuration()["loggingConfig"]
    return {
        "text_prompts": cfg.get("textDataDeliveryEnabled", False),
        "s3_archive": bool(cfg.get("s3Config")),
        "cloudwatch": bool(cfg.get("cloudWatchConfig")),
    }


def network(vpc_id: str) -> dict:
    c = session.client("ec2")
    subnets = c.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    ngw = c.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["NatGateways"]
    eps = c.describe_vpc_endpoints(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["VpcEndpoints"]
    return {
        "subnets": len(subnets),
        "azs": len({s["AvailabilityZone"] for s in subnets}),
        "nat_gateways": len([n for n in ngw if n["State"] != "deleted"]),
        "endpoints": [e["ServiceName"].split(".")[-1] for e in eps],
    }


def bedrock_models() -> dict:
    b = session.client("bedrock")
    ids = {m["modelId"] for m in b.list_foundation_models()["modelSummaries"]}
    profiles = {
        p["inferenceProfileId"] for p in b.list_inference_profiles()["inferenceProfileSummaries"]
    }
    return {
        "agent_model": {
            "id": "deepseek.v3.2",
            "available": "deepseek.v3.2" in ids,
            "tool_use": True,
            "invoke": "on-demand",
        },
        "summary_model": {
            "id": "us.deepseek.r1-v1:0",
            "available": "us.deepseek.r1-v1:0" in profiles,
            "tool_use": False,
            "invoke": "inference profile",
        },
        "embeddings": {
            "id": "amazon.titan-embed-text-v2:0",
            "available": "amazon.titan-embed-text-v2:0" in ids,
        },
        "total_visible": len(ids),
    }


def month_to_date_cost() -> dict:
    now = datetime.now(UTC)
    start = now.replace(day=1).strftime("%Y-%m-%d")
    resp = session.client("ce", region_name="us-east-1").get_cost_and_usage(
        TimePeriod={"Start": start, "End": now.strftime("%Y-%m-%d")},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    rows = []
    total = 0.0
    for g in resp["ResultsByTime"][0]["Groups"]:
        amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
        total += amt
        if amt > 0.005:
            rows.append({"service": g["Keys"][0], "usd": round(amt, 2)})
    return {
        "since": start,
        "total_usd": round(total, 2),
        "by_service": sorted(rows, key=lambda r: -r["usd"])[:8],
    }


# ---------------------------------------------------------------- repo stats


def plan_progress() -> list[dict]:
    """Parse PLAN.md phase headings + checkbox counts."""
    phases: list[dict] = []
    current: dict | None = None
    for line in (REPO / "PLAN.md").read_text().splitlines():
        if line.startswith("## "):
            if current:
                phases.append(current)
            title = line[3:].strip()
            if title.lower().startswith("non-goals"):
                current = None
                continue
            current = {
                "title": re.sub(r"\s+", " ", title.replace("✅", "")).strip(),
                "done": 0,
                "todo": 0,
            }
        elif current is not None:
            if line.strip().startswith("- [x]"):
                current["done"] += 1
            elif line.strip().startswith("- [ ]"):
                current["todo"] += 1
    if current:
        phases.append(current)
    return [p for p in phases if p["done"] + p["todo"] > 0]


def test_count() -> int:
    """Sum pytest's per-file collection counts (``path/to/test_x.py: 5``)."""
    out = subprocess.run(
        ["uv", "run", "pytest", "--collect-only", "-q"], capture_output=True, text=True, cwd=REPO
    )
    return sum(int(n) for n in re.findall(r"^\S+\.py: (\d+)$", out.stdout, re.M))


def code_stats() -> dict:
    def count(pattern: str, *roots: str) -> int:
        n = 0
        for root in roots:
            for f in (REPO / root).rglob(pattern):
                if any(part in {"venv", "__pycache__", ".venv"} for part in f.parts):
                    continue
                n += len(f.read_text(errors="ignore").splitlines())
        return n

    return {
        "core_lines": count("*.py", "packages/core/src"),
        "test_lines": count("*.py", "packages/core/tests"),
        "infra_lines": count("*.py", "pulumi/components") + count("__main__.py", "pulumi"),
    }


# ---------------------------------------------------------------- assemble


def main() -> int:
    stack, stack_err = probe(pulumi_stack)
    outs, outs_err = probe(stack_outputs)
    outs = outs or {}

    def out(key: str) -> str:
        return str(outs.get(key, ""))

    services = []

    def add(name: str, group: str, ident: str, value, err, note: str = "") -> None:
        services.append(
            {
                "name": name,
                "group": group,
                "id": mask(ident),
                "data": value,
                "error": err,
                "note": note,
            }
        )

    v, e = probe(network, out("vpc_id"))
    add("VPC", "Network", out("vpc_id"), v, e, "no NAT — zero egress cost")

    for table in ("jobs_table", "conversations_table"):
        v, e = probe(dynamo, out(table))
        add(out(table).split("-")[-1].title(), "Data", out(table), v, e)

    v, e = probe(s3_bucket, out("documents_bucket"))
    add("Documents bucket", "Data", out("documents_bucket"), v, e)

    v, e = probe(s3_bucket, out("kb_source_bucket"))
    add("KB source bucket", "Knowledge base", out("kb_source_bucket"), v, e)

    v, e = probe(knowledge_base, out("knowledge_base_id"), out("kb_data_source_id"))
    add("Knowledge Base", "Knowledge base", out("knowledge_base_id"), v, e)

    v, e = probe(vectors, "docpipe-dev-vectors", "docpipe-dev-kb")
    add(
        "S3 Vectors index",
        "Knowledge base",
        "docpipe-dev-vectors/docpipe-dev-kb",
        v,
        e,
        "serverless — replaces a ~$345/mo OpenSearch floor",
    )

    v, e = probe(sqs, out("queue_url"))
    add("Summaries queue", "Messaging", out("queue_url").split("/")[-1], v, e)

    v, e = probe(guardrail, out("guardrail_id"))
    add(
        "Bedrock guardrail",
        "Safety",
        out("guardrail_id"),
        v,
        e,
        "platform-level, not just a system prompt",
    )

    v, e = probe(invocation_logging)
    add("Invocation logging", "Safety", "account singleton", v, e, "every prompt/response archived")

    models, models_err = probe(bedrock_models)
    cost, cost_err = probe(month_to_date_cost)

    report = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "stack": {
            "name": "docpipe/dev",
            "region": REGION,
            "inventory": stack,
            "error": stack_err or outs_err,
        },
        "services": services,
        "models": models,
        "models_error": models_err,
        "cost": cost,
        "cost_error": cost_err,
        "phases": plan_progress(),
        "tests": test_count(),
        "code": code_stats(),
        "pending": [
            {"name": "API Lambda + REST API Gateway", "phase": "Phase 3"},
            {"name": "Strands agent (deepseek.v3.2 + search_kb tool)", "phase": "Phase 3"},
            {"name": "ConversationStore (DynamoDB)", "phase": "Phase 3"},
            {
                "name": "health.studio corpus ingestion",
                "phase": "Phase 3",
                "note": "awaiting corpus",
            },
            {
                "name": "Async worker compute",
                "phase": "Phase 5",
                "note": "Lambda vs Fargate undecided",
            },
            {
                "name": "Aurora pgvector benchmark",
                "phase": "Phase 5b",
                "note": "enable_aurora=false",
            },
        ],
    }

    (REPO / "status.json").write_text(json.dumps(report, indent=2) + "\n")

    template = (REPO / "scripts" / "dashboard.template.html").read_text()
    html = template.replace("/*DATA*/null", json.dumps(report))
    (REPO / "dashboard.html").write_text(html)

    live = sum(1 for s in services if not s["error"])
    print(f"status.json + dashboard.html written — {live}/{len(services)} services healthy")
    if stack:
        print(f"  pulumi: {stack['total']} resources")
    if cost:
        print(f"  cost MTD: ${cost['total_usd']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
