#!/usr/bin/env python3
"""Bootstrap Aurora for use as a Bedrock Knowledge Base vector store.

Pulumi creates the cluster and an empty Secrets Manager secret; it cannot create
a Postgres extension, role, schema, table or index, because there is no SQL
provider in this stack. This script is that missing half, and it is idempotent —
run it as many times as you like.

    AWS_PROFILE=docpipe python scripts/aurora_bootstrap.py            # apply
    AWS_PROFILE=docpipe python scripts/aurora_bootstrap.py --dry-run  # print SQL

It runs over the **RDS Data API** with the cluster's *master* secret, so it needs
no VPC access, no bastion and no NAT — the same reason Bedrock itself can reach a
cluster sitting in private subnets with no egress.

What it does, in order:

1. ``CREATE EXTENSION IF NOT EXISTS vector`` (pgvector >= 0.5.0, ships with 16.1+)
2. generates a password, creates/updates the ``bedrock_user`` Postgres role, and
   writes the credential to the Secrets Manager secret Pulumi allocated — both in
   the same run, so the secret and the role cannot disagree
3. creates ``bedrock_integration.bedrock_kb`` and its **three** indexes
4. grants ``bedrock_user`` exactly what Bedrock needs on that schema

The password is generated here rather than in Pulumi on purpose: it never enters
Pulumi state, and nothing but Secrets Manager and Postgres ever holds it.

**The first statement may take ~15s and that is expected, not a hang.** At
``min_capacity = 0`` the cluster auto-pauses when idle, and the first Data API
call after a pause pays the resume. That cost is an accepted, recorded decision
(FINDINGS.md, 2026-08-13 / 2026-08-14) — it also lands on the user-facing
retrieval path, not just here.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

# Bedrock's contract. These names are mirrored in
# pulumi/components/kb_aurora.py's field_mapping — change one, change both, or
# ingestion fails at write time with a column that does not exist.
SCHEMA = "bedrock_integration"
TABLE = "bedrock_kb"
DIMENSION = 1024  # Titan v2 @ 1024, pinned identically in both KBs.


def ddl(username: str) -> list[tuple[str, str]]:
    """(description, statement) pairs. Every statement is idempotent."""
    qualified = f"{SCHEMA}.{TABLE}"
    return [
        ("pgvector extension", "CREATE EXTENSION IF NOT EXISTS vector;"),
        ("schema", f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};"),
        (
            "table",
            # `id` is uuid PRIMARY KEY: Bedrock generates the ids, we never do.
            # `metadata` is json and `custom_metadata` is jsonb — that asymmetry
            # is Bedrock's, not a typo. Only jsonb can carry a GIN index, which
            # is why the sidecar attributes must land in custom_metadata.
            f"""CREATE TABLE IF NOT EXISTS {qualified} (
                   id uuid PRIMARY KEY,
                   embedding vector({DIMENSION}),
                   chunks text,
                   metadata json,
                   custom_metadata jsonb
               );""",
        ),
        (
            "HNSW vector index",
            # ef_construction=256 is the value AWS documents for this table.
            # hnsw.ef_search is a SESSION-level knob, not baked in here — which
            # is exactly what makes the Phase 5b recall/latency sweep possible
            # without an index rebuild.
            f"""CREATE INDEX IF NOT EXISTS {TABLE}_embedding_idx
                ON {qualified} USING hnsw (embedding vector_cosine_ops)
                WITH (ef_construction = 256);""",
        ),
        (
            "GIN full-text index",
            # This is what makes HYBRID search work on this store. Whether
            # S3 Vectors supports HYBRID at all is still unverified.
            f"""CREATE INDEX IF NOT EXISTS {TABLE}_chunks_fts_idx
                ON {qualified} USING gin (to_tsvector('simple', chunks));""",
        ),
        (
            "GIN metadata index",
            f"""CREATE INDEX IF NOT EXISTS {TABLE}_custom_metadata_idx
                ON {qualified} USING gin (custom_metadata);""",
        ),
        # One statement per entry: the Data API rejects multi-statement SQL
        # ("Multistatements aren't supported" — hit on first run, 2026-08-20).
        (
            "grant: schema usage",
            f"GRANT USAGE ON SCHEMA {SCHEMA} TO {username};",
        ),
        (
            "grant: table DML",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {qualified} TO {username};",
        ),
    ]


def stack_outputs() -> dict[str, str]:
    """Read the dev stack's outputs. Fails loudly rather than guessing ARNs."""
    try:
        raw = subprocess.run(
            ["pulumi", "stack", "output", "--json", "--stack", "dev"],
            cwd="pulumi",
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except FileNotFoundError:
        sys.exit("pulumi CLI not found — needed to resolve the cluster ARN.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"`pulumi stack output` failed:\n{e.stderr}")
    return json.loads(raw)


def generate_password(length: int = 32) -> str:
    """Postgres-safe: no quotes, no backslash, nothing that needs escaping."""
    alphabet = string.ascii_letters + string.digits + "-_.~"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def execute(client, cluster_arn: str, secret_arn: str, database: str, sql: str) -> dict:
    """One Data API statement, with a retry for the auto-pause resume."""
    for attempt in range(3):
        try:
            return client.execute_statement(
                resourceArn=cluster_arn,
                secretArn=secret_arn,
                database=database,
                sql=sql,
            )
        except ClientError as e:
            code = e.response["Error"]["Code"]
            # The cluster is resuming from min_capacity=0. Documented ~15s.
            if code in ("DatabaseResumingException", "BadRequestException") and attempt < 2:
                wait = 10 * (attempt + 1)
                print(f"    cluster resuming ({code}) — waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="print the SQL, touch nothing")
    ap.add_argument("--region", default="us-east-1")
    args = ap.parse_args()

    out = stack_outputs()
    missing = [
        k
        for k in (
            "aurora_cluster_arn",
            "aurora_master_secret_arn",
            "aurora_bedrock_secret_arn",
            "aurora_database_name",
            "aurora_bedrock_username",
        )
        if k not in out
    ]
    if missing:
        sys.exit(
            "Stack is missing Aurora outputs: "
            + ", ".join(missing)
            + "\nSet `enableAurora: true` and run `pulumi up` first."
        )

    cluster_arn = out["aurora_cluster_arn"]
    master_secret = out["aurora_master_secret_arn"]
    bedrock_secret = out["aurora_bedrock_secret_arn"]
    database = out["aurora_database_name"]
    username = out["aurora_bedrock_username"]

    statements = ddl(username)

    if args.dry_run:
        print(f"-- cluster:  {cluster_arn}")
        print(f"-- database: {database}\n")
        print(f"-- role {username} is created with a generated password (not shown)\n")
        for desc, sql in statements:
            print(f"-- {desc}\n{sql}\n")
        return 0

    rds = boto3.client("rds-data", region_name=args.region)
    sm = boto3.client("secretsmanager", region_name=args.region)

    password = generate_password()

    # Role first: the grants at the end reference it. DO block because Postgres
    # has no CREATE ROLE IF NOT EXISTS, and re-running must stay safe.
    print(f"==> role {username}")
    execute(
        rds,
        cluster_arn,
        master_secret,
        database,
        f"""DO $$
            BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{username}') THEN
                CREATE ROLE {username} WITH LOGIN PASSWORD '{password}';
              ELSE
                ALTER ROLE {username} WITH LOGIN PASSWORD '{password}';
              END IF;
            END
            $$;""",
    )

    # Write the credential only after the role actually exists, and in the shape
    # Bedrock expects. Doing it in this order means a failure leaves the secret
    # empty rather than holding a password no role answers to.
    print("==> secret (username/password → Secrets Manager)")
    sm.put_secret_value(
        SecretId=bedrock_secret,
        SecretString=json.dumps({"username": username, "password": password}),
    )

    for desc, sql in statements:
        print(f"==> {desc}")
        execute(rds, cluster_arn, master_secret, database, sql)

    print(
        "\nBootstrap complete. Now set `enableAuroraKb: true` and run `pulumi up`\n"
        "to create the Aurora-backed Knowledge Base."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
