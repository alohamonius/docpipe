# Build plan

Phased so every phase leaves the repo in a working, demonstrable state.

**Goal:** a real AWS-hosted AI backend for health.studio — an agent-based chat
assistant first, async summaries second — demonstrating the full stack
(Bedrock, Lambda, DynamoDB, API Gateway, S3, Aurora, VPC, IAM, CloudWatch,
SQS, Pulumi).

**Two product pillars:** everything is **per-user** (opaque IDs from the app,
DynamoDB keyed `userId` + `conversationId`/`jobId`), and chat is **grounded in
the health.studio knowledge base** (public, evidence-graded anatomy/exercise
content) via Bedrock Knowledge Bases + S3 Vectors, with citations.

**Locked decisions (2026-08-13):**
- **IaC = Pulumi (Python)** — the Terraform tree was ported and deleted.
- **Agent = Strands Agents SDK running in the API Lambda.** Bedrock Agents
  Classic is closed to new customers (see FINDINGS.md); AgentCore is the
  managed alternative but is heavier than chat needs — Strands code can move
  into AgentCore Runtime later without a rewrite.
- **Agent model = `deepseek.v3.2`** (tool use via Converse verified, ON_DEMAND,
  no inference profile). **R1 (`us.deepseek.r1-v1:0`) stays for async
  summaries** — it has no tool use.
- KB corpus location: TBD — collector agents are still gathering content.

## Phase 0 — Init & setup ✅

- [x] Repo skeleton: packages / services / infra layout
- [x] uv workspace, `docpipe-core` package stub, pytest wired
- [x] Terraform skeleton: modules, dev env, versions, backend example
- [x] .gitignore covering tfstate / tfvars / .env / caches
- [x] Makefile with fmt / lint / test / infra targets

## Phase 1 — Shared package (`docpipe-core`) ✅

The heart of the demo: one library reused by the Lambda and the worker.
Built on the model-agnostic Converse API — swappable by config, no vendor SDK.
(Built against **DeepSeek-R1**; chat/agent since moved to `deepseek.v3.2` for
tool use, R1 remains the summaries model.)

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
- [x] `retrieval.py` — `KnowledgeBaseClient` (Bedrock KB retrieve API),
      `RetrievedPassage`; `ChatClient` grounding: passages injected into the
      system prompt with cite-or-say-so instructions
- [x] User scoping in models: `user_id` on `Job` and `Conversation`

## Phase 2 — Infra foundation ✅ (deployed 2026-08-13, Pulumi)

Originally built as Terraform modules, then ported to **Pulumi (Python)** in
`pulumi/` (one `ComponentResource` per concern + two boto3 dynamic providers
for pulumi-aws gaps: S3 Vectors, invocation logging). Terraform deleted.
`pulumi up` on the dev stack: **53 resources live** — KB `SJQAFQXPH7`,
guardrail `pjpeeu9hf68a`, invocation logging active. Gotchas in FINDINGS.md.

- [x] `network` module: VPC, public/private subnets ×2 AZ, **no NAT** (nothing
      needs VPC egress), S3 + DynamoDB gateway endpoints (free)
- [x] `iam` module: per-service roles, least-privilege policies (api-lambda:
      S3 put + DDB write + SQS send + KB retrieve + Bedrock invoke; worker
      policy: SQS consume + Bedrock invoke + DDB — trust/IRSA deferred to Phase 5)
- [x] `data` module: S3 bucket (versioned, SSE), DynamoDB tables (`jobs` PK
      userId SK jobId + TTL; `conversations` PK userId SK conversationId + TTL),
      Aurora Serverless v2 (min ACU, private subnets) — **Aurora gated behind
      `enable_aurora` (default false) so the cheap chat path can apply alone**
- [x] `kb` module: KB source S3 bucket, Bedrock Knowledge Base (Titan
      embeddings) backed by **S3 Vectors** (cheap, serverless — not OpenSearch)
- [x] `messaging` module: SQS queue + DLQ + redrive policy
- [x] Remote state: self-managed S3 backend (`pulumi login s3://…?region=…`)
- [x] `make infra-preview` / `infra-up` / `infra-down` for the dev stack

## Phase 3 — API service (Lambda + API Gateway) — the agent ships here

- [x] KB content sync: `docpipe_core.kb_sync.CorpusSyncer` (walk `*.md` →
      upload to KB source S3, incremental by content-MD5/ETag → `StartIngestion
      Job` + poll) with a thin CLI at `services/kb_sync/sync.py` (flags → env →
      `terraform output` resolution, `--dry-run`/`--no-ingest`/`--no-wait`).
      8 unit tests (moto S3 + fake bedrock-agent), mypy/ruff clean.
- [ ] **Agent core**: Strands Agents SDK agent on `deepseek.v3.2` — tools:
      `search_kb` (KB retrieve), conversation history; non-diagnostic health
      system prompt; citations required for KB-sourced claims
- [ ] `services/api`: handlers for `POST /chat` (runs the agent, conversation
      persisted per user), `POST /summarize`, `GET /jobs/{id}` using core
- [ ] `ConversationStore` in core (DynamoDB, PK userId SK conversationId, TTL)
- [ ] Auth + rate limiting (**locked design** — no Cognito): **REST API Gateway**
      + **API key + usage plan** (rate/burst + **daily quota** = hard Bedrock cost
      cap; health.studio holds the key), opaque `userId` required on every route,
      plus a **per-user token-bucket in DynamoDB** in the Lambda (usage plans
      throttle per-key, not per-user). End-user login is Auth.js/Google in the
      app; the browser never calls docpipe, so the app-asserted `userId` is safe.
- [ ] Packaging: zip build via uv export → CI artifact
- [ ] `api-lambda` module: **REST API Gateway** (needed for usage-plan quotas),
      routes, Lambda, log group, method throttling
- [ ] Smoke test script hitting the deployed endpoint

### Safety & audit (must exist before the first real Bedrock invoke)

- [x] **Bedrock Guardrail** (`aws_bedrock_guardrail`, `safety` module): denied
      topics (diagnosis / prescription), PROMPT_ATTACK + content filters, PII
      anonymize, grounding/relevance thresholds. Platform-level, not just the
      system prompt. Outputs `guardrail_id` + `guardrail_version` (DRAFT).
- [x] **Model invocation logging**
      (`aws_bedrock_model_invocation_logging_configuration`): every
      prompt/response → S3 (SSE, bedrock-scoped bucket policy) + CloudWatch
      (retention-capped, via a Bedrock-assumed role).
- [ ] Wire `guardrail_id`/`version` into the `ChatClient`/`SummarizerClient`
      Converse calls (`docpipe-core`) — Phase 3, when the handlers are built.

## Phase 4 — health.studio integration (chat live end-to-end)

In the health.studio repo, as soon as `/chat` is deployed — the app gets
real value before the heavier worker/benchmark work starts.

- [ ] End-user login: **Auth.js (NextAuth) + Google** in health.studio; the
      server derives the opaque `userId` from the session and forwards it to
      docpipe (no Cognito — identity stays in the app, off AWS)
- [ ] Server-side route `/api/assistant` in health.studio calling this API
      (API key from env; never exposed to the browser)
- [ ] Feature-flagged chat UI in the app; reuse existing disclaimer +
      red-flag-gate components
- [ ] Privacy pass: opaque conversation IDs only, no user PII in payloads
- [ ] E2E: message from the app → API Gateway → Bedrock → reply rendered

## Phase 5 — Async worker (compute TBD)

**Open decision: EKS vs Lambda vs Fargate.** EKS was chosen to demonstrate the
stack, but the control plane is ~$73/mo *just to exist* — the biggest standing
cost in the whole design, for an occasional summary job. Options:
- **Lambda** — SQS event source, 15-min max timeout (covers LLM summaries),
  ~$0 at rest. Simplest, cheapest. Default recommendation.
- **Fargate** — pay-per-task container, no idle control-plane cost. If a job
  might exceed 15 min or needs a long-lived container.
- **EKS** — only if the *portfolio* goal specifically needs "I ran Kubernetes."
  The benchmark below is a stronger flex than EKS-for-its-own-sake.

- [ ] Decide worker compute (above); the `worker` IAM policy already attaches
      to whichever — only the trust/role differs
- [ ] `services/worker`: SQS consumer (FastAPI+poll for Fargate/EKS, or a plain
      handler for Lambda), reusing `docpipe-core`
- [ ] End-to-end: POST doc → summary lands in DynamoDB + Aurora

## Phase 5b — Headline deliverable: S3 Vectors vs Aurora pgvector benchmark (proposed)

Not "I built a RAG pipeline on Bedrock" — instead: **"I benchmarked S3 Vectors
against Aurora pgvector on the same corpus and golden set — here's retrieval
latency, cost, and where each wins."** The measured decision *is* the artifact.

- [ ] Ingest the identical health.studio corpus into **both** stores (S3 Vectors
      index — already built; Aurora pgvector — `enable_aurora` + `vector` column)
- [ ] Golden set: ~30–50 `question → expected-passage(s)` pairs from the corpus
- [ ] Measure per store: retrieval **latency** (p50/p95, incl. Aurora cold-start
      after auto-pause), **recall@k / MRR** on the golden set, **$ cost** (at rest
      + per 1k queries)
- [ ] Writeup: a results table + "where each wins" — the README's real headline

## Phase 6 — Observability & polish

- [ ] `monitoring` module: CloudWatch dashboard, alarms (DLQ depth > 0,
      Lambda errors, worker restarts), SNS email
- [ ] Cost backstop: **AWS Budgets alarm on Bedrock spend** + CloudWatch alarm
      on invocation-count spikes (catches abuse before the bill does)
- [ ] README: architecture diagram render, demo GIF/recording, cost table
- [ ] `terraform destroy` clean-pass verified (no orphaned resources)

## Non-goals

- Multi-env promotion (only `dev`), custom domains, auth beyond API keys,
  multi-region. Noted in README as conscious scope cuts.
