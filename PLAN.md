# Build plan

Phased so every phase leaves the repo in a working, demonstrable state.

**Goal:** a real AWS-hosted AI backend for health.studio — chat first, async
summaries second — that also demonstrates the full stack (Bedrock, Lambda,
EKS, DynamoDB, API Gateway, S3, Aurora, VPC, IAM, CloudWatch, SQS, Terraform).

## Phase 0 — Init & setup ✅

- [x] Repo skeleton: packages / services / infra layout
- [x] uv workspace, `docpipe-core` package stub, pytest wired
- [x] Terraform skeleton: modules, dev env, versions, backend example
- [x] .gitignore covering tfstate / tfvars / .env / caches
- [x] Makefile with fmt / lint / test / infra targets

## Phase 1 — Shared package (`docpipe-core`) ✅

The heart of the demo: one library reused by Lambda and the EKS worker.
Model: **DeepSeek-R1 on Bedrock** (`us.deepseek.r1-v1:0`) via the
model-agnostic Converse API — swappable by config, no vendor SDK.

- [x] `models.py` — Pydantic: `Job`, `JobStatus`, `SummaryResult`
- [x] `storage.py` — S3 (put/get document) + DynamoDB (job CRUD) wrappers
- [x] `queue.py` — SQS publish/consume
- [x] `llm.py` — Bedrock Converse client: DeepSeek, retries/backoff,
      `<think>` scratchpad stripping, token usage capture
- [x] `observability.py` — structured JSON logging + CloudWatch EMF metrics
- [x] Unit tests with moto (S3/DynamoDB/SQS mocked); Bedrock behind a fake client
- [x] CI: GitHub Actions — ruff, mypy, pytest, terraform fmt
- [x] `chat.py`-level support: `ChatClient` (multi-turn Converse, non-diagnostic
      health system prompt), `ChatMessage`/`ChatReply` models

## Phase 2 — Terraform foundation

- [ ] `network` module: VPC, public/private subnets ×2 AZ, single NAT, endpoints
      (S3 + DynamoDB gateway endpoints — free, keeps traffic off the NAT)
- [ ] `iam` module: per-service roles, least-privilege policies (api-lambda:
      S3 put + DDB write + SQS send; worker: SQS consume + Bedrock invoke + DDB/RDS)
- [ ] `data` module: S3 bucket (versioned, SSE), DynamoDB tables (`jobs` PK
      jobId + TTL; `conversations` PK conversationId + TTL),
      Aurora Serverless v2 (min ACU, private subnets)
- [ ] `messaging` module: SQS queue + DLQ + redrive policy
- [ ] Remote state: S3 backend with native lockfile; `backend.hcl` per env
- [ ] `make infra-up` / `make infra-down` for the dev env

## Phase 3 — API service (Lambda + API Gateway) — chat ships here

- [ ] `services/api`: handlers for `POST /chat` (sync Bedrock call, conversation
      persistence in DynamoDB), `POST /summarize`, `GET /jobs/{id}` using core
- [ ] `ConversationStore` in core (DynamoDB, TTL-expired history)
- [ ] API-key auth on API Gateway (server-to-server; health.studio holds the key)
- [ ] Packaging: zip build via uv export → CI artifact
- [ ] `api-lambda` module: HTTP API Gateway, routes, Lambda, log group, throttling
- [ ] Smoke test script hitting the deployed endpoint

## Phase 4 — health.studio integration (chat live end-to-end)

In the health.studio repo, as soon as `/chat` is deployed — the app gets
real value before the heavier EKS work starts.

- [ ] Server-side route `/api/assistant` in health.studio calling this API
      (API key from env; never exposed to the browser)
- [ ] Feature-flagged chat UI in the app; reuse existing disclaimer +
      red-flag-gate components
- [ ] Privacy pass: opaque conversation IDs only, no user PII in payloads
- [ ] E2E: message from the app → API Gateway → Bedrock → reply rendered

## Phase 5 — Worker (EKS + Bedrock)

- [ ] `services/worker`: FastAPI app + SQS poll loop, Dockerfile
- [ ] `eks` module: cluster, one small managed node group, IRSA for the worker role
- [ ] Helm chart (or plain manifests) — deliberately *not* Terraform-managed
- [ ] End-to-end: POST doc → summary lands in DynamoDB + Aurora

## Phase 6 — Observability & polish

- [ ] `monitoring` module: CloudWatch dashboard, alarms (DLQ depth > 0,
      Lambda errors, worker restarts), SNS email
- [ ] README: architecture diagram render, demo GIF/recording, cost table
- [ ] `terraform destroy` clean-pass verified (no orphaned resources)

## Non-goals

- Multi-env promotion (only `dev`), custom domains, auth beyond API keys,
  multi-region. Noted in README as conscious scope cuts.
