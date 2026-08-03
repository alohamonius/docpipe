# docpipe — health.studio AI services on AWS

The AWS-hosted AI backend for [health.studio](https://health.studio): a chat
assistant and an async document-summarization pipeline, called server-side by
the health.studio Next.js app (which runs on its own infra). Built on:

**Bedrock (DeepSeek) · Lambda · EKS · DynamoDB · API Gateway · S3 · RDS (Aurora) · VPC · IAM · CloudWatch · SQS — provisioned with Terraform.**

## Two flows, one shared package

```
health.studio (Next.js, server-side)          health.studio knowledge base
        │  API key + opaque userId                 (docs/anatomy, articles —
        ▼                                           public, evidence-graded)
  API Gateway                                              │ sync
   ├── POST /chat ──────▶ Lambda ─┬▶ Bedrock KB (retrieve) ◀── S3 + S3 Vectors
   │                     [sync]   └▶ Bedrock (DeepSeek, grounded + cited)
   │                        └──▶ DynamoDB (userId → conversations)
   ├── POST /summarize ─▶ Lambda ──▶ S3 + DynamoDB + SQS     [async]
   │                                      │
   │                                      ▼
   │                              EKS worker ──▶ Bedrock
   │                                      │        │
   │                                     DLQ   DynamoDB + Aurora
   └── GET /jobs/{id} ──▶ Lambda ──▶ DynamoDB (userId → job status)
```

1. **Chat (sync, grounded)** — the app posts a conversation with an opaque
   `userId`; Lambda retrieves the top-k relevant passages from the **Bedrock
   Knowledge Base** (built from health.studio's public, evidence-graded
   anatomy/exercise-science corpus), then calls DeepSeek via the Converse API
   with those passages as grounding context. Replies cite their sources; when
   the KB doesn't cover a question, the assistant says so instead of guessing.
   Conversations persist per-user in DynamoDB.
2. **Summaries (async)** — training logs / session notes go to S3, a job is
   enqueued on SQS, the EKS worker summarizes via Bedrock and writes results
   to DynamoDB (status) and Aurora (per-user history). LLM calls are slow →
   they never block the request path.

The compute split is deliberate: Lambda for the spiky request edge, EKS for
the long-running consumer. Both reuse one Python package, `docpipe-core`.
The KB vector store is **S3 Vectors** (not OpenSearch Serverless) so the
whole chat path stays near-zero cost at rest.

## Health & privacy constraints

- The assistant is **non-diagnostic**: system prompt forbids diagnosis and
  prescriptions, and directs red-flag symptoms to a clinician (mirrors the
  red-flag gate already in the health.studio app).
- **Grounded, not vibes**: answers are anchored to the same evidence-graded,
  publicly-sourced knowledge base the app publishes — with citations — and
  the assistant admits when the KB doesn't cover a topic.
- **No PII/PHI leaves health.studio**: the app sends anonymous conversation
  content keyed by opaque user/conversation IDs — no names, emails, or
  account data. The KB contains only public content.
- All calls are server-to-server; the browser never talks to this API.

## Repo layout

```
packages/core/     docpipe-core: models, storage, queue, Bedrock chat +
                   summarization clients, observability. Reused everywhere.
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
make fmt lint typecheck        # ruff + mypy, terraform fmt
```

## Cost notes

This stack is not free-tier: EKS control plane (~$73/mo), NAT gateway
(~$32/mo), Aurora. The dev environment is built to be created and destroyed
per session (`make infra-up` / `make infra-down`), with Aurora Serverless v2
at minimum ACU and a single NAT gateway. The sync chat path alone (API
Gateway + Lambda + Bedrock + DynamoDB) costs near-zero at rest — it can stay
up permanently while the EKS/Aurora side is torn down between sessions.
