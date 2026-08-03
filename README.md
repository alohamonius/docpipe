# docpipe

Async document summarization pipeline on AWS. A small, production-shaped system
built to demonstrate a modern AWS stack end to end:

**Bedrock · Lambda · EKS · DynamoDB · API Gateway · S3 · RDS (Aurora) · VPC · IAM · CloudWatch · SQS — provisioned with Terraform.**

## What it does

```
POST /summarize (document)                         GET /jobs/{id}
        │                                                │
        ▼                                                ▼
  API Gateway ──▶ Lambda (api) ──▶ S3 (raw doc)    DynamoDB (job status)
                      │                                  ▲
                      ▼                                  │
                     SQS ──▶ EKS worker ──▶ Bedrock ─────┤
                      │         (FastAPI       (Claude)  │
                      ▼          consumer)               │
                     DLQ                    Aurora ◀─────┘
                                        (job history)
```

1. Client `POST`s a document. The **Lambda** API handler stores it in **S3**,
   creates a job record in **DynamoDB**, and enqueues a message on **SQS**.
2. A long-running consumer on **EKS** picks up the message, calls **Bedrock**
   (Claude) to summarize, writes the result to DynamoDB and relational history
   to **Aurora**.
3. Client polls `GET /jobs/{id}` for status/result.
4. Failures retry via SQS; poison messages land in a **DLQ** with a
   **CloudWatch** alarm on queue depth.

The compute split is deliberate: Lambda for the spiky, cheap API edge;
EKS for the long-running LLM consumer. DynamoDB for fast status polling;
Aurora for relational history. SQS because LLM calls are slow and must not
block the request path.

## Repo layout

```
packages/core/     Shared Python package (docpipe-core): models, storage,
                   queue, Bedrock client, observability. Used by every service.
services/api/      Lambda handlers behind API Gateway.
services/worker/   FastAPI SQS consumer, containerized, deployed to EKS.
infra/             Terraform. Reusable modules + a dev environment.
```

## Infra / secrets split

Everything in this repo is public by design. What never enters git:

- `terraform.tfstate` — lives in a remote S3 backend (native lockfile, TF ≥ 1.10)
- real `*.tfvars` — only `terraform.tfvars.example` is committed
- credentials — AWS auth via SSO/environment, never in code

See [`infra/README.md`](infra/README.md) for the module map and
[`PLAN.md`](PLAN.md) for the build phases and current status.

## Dev quickstart

```bash
uv sync --all-packages         # install workspace incl. dev deps (Python 3.12)
uv run pytest                  # shared package tests
make fmt lint                  # ruff format + check, terraform fmt
```

## Cost notes

This stack is not free-tier: EKS control plane (~$73/mo), NAT gateway
(~$32/mo), Aurora. The dev environment is built to be created and destroyed
per session (`make infra-up` / `make infra-down`), with Aurora Serverless v2
at minimum ACU and a single NAT gateway. LocalStack covers local dev for
everything except Bedrock and EKS.
