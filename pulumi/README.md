# docpipe infrastructure — Pulumi (Python)

Clean Python rebuild of the earlier Terraform stack. One `ComponentResource` per
concern in `components/`, wired in `__main__.py`.

| Component | Resources |
|-----------|-----------|
| `network` | VPC, public/private subnets ×2 AZ, **no NAT**, free S3/DynamoDB gateway endpoints |
| `data`    | S3 documents bucket (versioned/SSE/lifecycle), DynamoDB `jobs` + `conversations` (PK userId, TTL, PITR), optional Aurora Serverless v2 (`min_acu=0` auto-pause) |
| `messaging` | SQS main queue + DLQ + redrive |
| `kb`      | KB source bucket, **S3 Vectors** store, Bedrock Knowledge Base (Titan v2, 1024-dim pinned) |
| `iam`     | API Lambda role + standalone worker policy (least-privilege) |
| `safety`  | Bedrock guardrail (no diagnosis/prescription, PII-anonymize, grounding) + model invocation logging → S3/CloudWatch |

## Prerequisites

- **Pulumi CLI** ≥ 3.140 — `brew install pulumi` (or the official installer).
- **Python venv** — Pulumi creates it from `requirements.txt` on first `up`.
- **AWS auth** — uses the `docpipe` profile (set in `Pulumi.dev.yaml`).
- **Classic Bedrock enabled** on the account — the `kb` + `safety` (guardrail)
  resources need it. This is the AWS support-case item; the non-Bedrock
  resources (network/data/messaging/iam) deploy without it.

## State backend (S3, self-managed — no Pulumi Cloud)

```bash
# The region query param is required — a bare s3:// URL 301s on a non-default region.
pulumi login "s3://docpipe-tfstate-<ACCOUNT_ID>/pulumi?region=us-east-1&awssdk=v2&profile=docpipe"
export PULUMI_CONFIG_PASSPHRASE=""   # no secrets in config yet; set a real one before adding any
```

(Reuses the existing state bucket under a `pulumi/` prefix. Substitute your own
AWS account id for `<ACCOUNT_ID>`.)

## Commands

```bash
cd pulumi
pulumi stack init dev            # first time only
pulumi preview                   # dry run — VERIFY here first (see note below)
pulumi up                        # create/update
pulumi stack output              # the .env values
pulumi destroy                   # clean teardown (near-zero-at-rest discipline)
```

## Two boto3-backed dynamic providers (pulumi-aws gaps)

pulumi-aws 7.40 has the Bedrock KB + Guardrail natively, but **not** the S3
Vectors store or invocation logging (Terraform's provider did). Those two are
filled by small boto3-backed Pulumi **dynamic providers**, managed in the same
`up` / `destroy` lifecycle:

- `components/s3vectors.py` — creates the S3 Vectors bucket + index; exposes the
  index ARN the KB references. (boto3 `s3vectors`.)
- `components/invocation_logging.py` — sets the account-level Bedrock invocation
  logging config. (boto3 `bedrock.put_model_invocation_logging_configuration`.)

Everything else is native pulumi-aws. Guardrail + KB nested arg names were
verified against the installed SDK; a `pulumi preview` sanity pass on the KB
nested config is still worth doing on the first run.

## Config (`Pulumi.dev.yaml`)

| Key | Default | Meaning |
|-----|---------|---------|
| `aws:region` | `us-east-1` | region |
| `aws:profile` | `docpipe` | local profile (unset in CI) |
| `docpipe:namePrefix` | `docpipe-dev` | resource name prefix |
| `docpipe:enableAurora` | `false` | Phase 5b pgvector benchmark cluster |
| `docpipe:bedrockModelId` | `us.deepseek.r1-v1:0` | chat inference profile |

## Retiring Terraform

Once `pulumi up` succeeds, the old `infra/` (Terraform) directory is dead and
should be removed (`git rm -r infra/`). Until then it stays as the reference the
components were translated from.
