# Build plan

Phased so every phase leaves the repo in a working, demonstrable state.

**Goal:** a real AWS-hosted AI backend for health.studio — an agent-based chat
assistant first, async summaries second — demonstrating the full stack
(Bedrock, Lambda, DynamoDB, API Gateway, S3, Aurora, VPC, IAM, CloudWatch,
SQS, Pulumi).

**Two product pillars:** everything is **per-user** (opaque IDs from the app,
DynamoDB keyed `userId` + `conversationId`/`jobId`), and chat is **grounded in
the health.studio knowledge base** (public, evidence-graded anatomy/exercise
content) via Bedrock Knowledge Bases, with citations.

**Locked decisions (2026-08-13, revised after the verification pass):**
- **IaC = Pulumi (Python)** — the Terraform tree was ported and deleted.
- **Agent = Strands Agents SDK running in the API Lambda.** Bedrock Agents
  Classic is closed to new customers (see FINDINGS.md). The stronger argument is
  licensing, not availability: **Strands is Apache-2.0 and runs anywhere;
  AgentCore is a closed AWS-only managed service.** AgentCore Harness (GA
  2026-06-18) exports to Strands code with one CLI command, so this is not a
  one-way door. At our shape — one tool, one model, a guardrail on every call —
  Harness would manage complexity we do not have.
- **Agent model = `deepseek.v3.2`** (tool use via Converse verified, ON_DEMAND,
  no inference profile). **R1 (`us.deepseek.r1-v1:0`) stays for async
  summaries** — it has no tool use. Fallback if v3.2 disappoints on tool use:
  the Claude 5 family is available in-account, all `INFERENCE_PROFILE` only.
- **DeepSeek only — one family, two models, locked 2026-08-14 (the human's).**
  Z.AI's GLM was evaluated against the same bar and dropped: `zai.glm-5`,
  `zai.glm-4.7` and `zai.glm-4.7-flash` are all live and ON_DEMAND in-account,
  all three pass tool use via Converse, and flash is ~9× cheaper on input than
  `deepseek.v3.2` (FINDINGS.md, 2026-08-14). It was rejected anyway, to keep one
  model family rather than to win a price comparison. Two consequences:
  a free/pro split by model is off the table (R1 has no tool use, so DeepSeek
  offers no cheap chat sibling) — tier on `top_k`, `min_evidence` and quota
  instead; and the IAM allowlist names its two models rather than matching
  `deepseek*`, so adding one is a reviewed change.
- **Embedding model = `amazon.titan-embed-text-v2:0` @ 1024 dims — FORCED, not
  preferred.** Bedrock KB **rejects `cohere.embed-v4:0`** under every storage
  type (measured, FINDINGS.md), and the only other supported family — Cohere
  Embed v3 — caps input at 512 tokens against a corpus at p90 ≈ 2,200 tokens.
  Titan v2's 8,192 is the only fit. Do not reopen without re-measuring.
- **Vector store = Aurora PostgreSQL + pgvector as primary; S3 Vectors retained
  as a second KB over the same source bucket.** `min_capacity = 0` **is**
  supported by Bedrock KB (ACU 0–16), so scale-to-zero survives; the ~15s resume
  is accepted and hidden by pre-warming on chat-UI open. Two KBs make Phase 5b a
  config flip (`retrieval.py` already takes `knowledge_base_id`) and double as a
  rollback path. S3 Vectors idles at ~$0, so keeping it costs nothing.
- **KB corpus = `health.studio/build/kb/`**, produced by `pnpm kb:build` — **383
  chunk-final files + 383 `.metadata.json` sidecars** (192 prose from 20 docs,
  191 graph; 1,686,867 B). **Not `docs/`**: that tree holds files health.studio's
  corpus policy excludes, one of them for a safety reason. Because the corpus is
  chunk-final, the data source runs `chunkingStrategy: NONE` — see FINDINGS.md.

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
- [ ] **Aurora gaps found 2026-08-13, required before it can back a KB:**
      `enable_http_endpoint` (RDS Data API) is **not set** in `data.py`; a
      dedicated `bedrock_user` role + its own Secrets Manager secret is needed
      (the current `manage_master_user_password` is the *master* secret); and
      the `bedrock_integration.bedrock_kb` table + **three** indexes (HNSW
      `ef_construction=256`, GIN on `to_tsvector`, GIN on `custom_metadata`)
      must be bootstrapped — Pulumi will not create them. Schema in FINDINGS.md.
- [ ] **Untracked resource:** Managed KB `XHWRWMWMIQ`
      (`knowledge-base-quick-start-nxl5n`, created 2026-08-04 via console Quick
      Create) is live and outside Pulumi state — `make infra-down` will not
      remove it. Keep as a Managed-KB comparison point, or delete.
- [x] `messaging` module: SQS queue + DLQ + redrive policy
- [x] Remote state: self-managed S3 backend (`pulumi login s3://…?region=…`)
- [x] `make infra-preview` / `infra-up` / `infra-down` for the dev stack

## Phase 3 — API service (Lambda + API Gateway) — the agent ships here

### Pre-flight — ordered, and all of it is free only while the KB is empty

The source bucket holds **0 objects** and the KB has run **0 ingestion jobs**, so
every corpus-shape decision is still reversible at zero cost. That stops being
true after the first ingest. Do these in order:

- [ ] **BLOCKER — deployed config ≠ code.** The live data source `KPAQK6MQY4`
      still reports `FIXED_SIZE` (500 tokens / 20% overlap); the `NONE` change in
      `pulumi/components/kb.py` is uncommitted and unapplied. Ingesting now
      strips the ★ legend and the "not a diagnosis" header off every fragment
      after the first. Commit → `pulumi up` → **then** sync. Note the `up`
      **replaces** the data source, so `dataSourceId` changes.
- [ ] **Split the 19 oversized graph chunks upstream** (health.studio
      `build-kb.ts`). Measured: prose is healthy (median 264w, max 664w) but
      graph runs median 652w to **max 3,692w** — one 1024-dim vector per file
      means a whole organ's referral map gets the same budget as a 107-word
      overview. Splitting changes filenames → S3 keys, so doing it *after* an
      ingest creates orphan vectors. Do it first and that problem never exists.
- [ ] Re-run `--dry-run` to confirm the new distribution, then one ingest.

### Known gaps in the sync path

- [ ] **`CorpusSyncer` never deletes.** `plan()` walks local files and uploads or
      skips; nothing prunes. Renamed/renumbered chunks leave live vectors that
      retrieve as current text forever. Bedrock *does* detect S3 deletions — the
      gap is that we never delete the object. Better fix found since:
      `ListKnowledgeBaseDocuments` + `DeleteKnowledgeBaseDocuments` (accepts
      `dataSourceType: S3` + a plain `uri`) let us reconcile at the **KB** level,
      and `IngestKnowledgeBaseDocuments` pushes a delta without a full-bucket
      scan. Batch-limited per call — check the cap before building on it.
- [ ] **No scheduled refresh exists.** Every `bedrock-agent` operation was
      enumerated: there is no scheduling API. The cadence is ours to build —
      EventBridge Scheduler → Lambda → `StartIngestionJob`, or trigger from
      health.studio CI when `pnpm kb:build` output changes. Re-embedding is
      incremental and the full corpus is ~420k tokens (≈1¢ at Titan v2 rates),
      so **cost is not a constraint on cadence** — do not design around it.

### Retrieval levers we provision nothing for (all no-reingest)

- [ ] **Reranker** — `cohere.rerank-v3-5:0` is ON_DEMAND in-account and
      `KnowledgeBaseVectorSearchConfiguration` carries `rerankingConfiguration`.
      Query-time counter to chunk-size dilution; the only quality lever here that
      needs no rebuild. Billed per query, not per token. Rate unverified — the
      Price List API carries no rerank SKU.
- [ ] **Hybrid search** — `overrideSearchType: HYBRID|SEMANTIC`. Aurora
      provisions a GIN `to_tsvector` index for exactly this; **unverified whether
      S3 Vectors supports HYBRID at all.**
- [ ] **`implicitFilterConfiguration`** — a model derives metadata filters from
      the query text, using sidecar attributes we already ship.

- [x] KB content sync: `docpipe_core.kb_sync.CorpusSyncer` (walk `*.md` **+ each
      one's `.metadata.json` sidecar** → upload to KB source S3, incremental by
      content-MD5/ETag → `StartIngestionJob` + poll) with a thin CLI at
      `services/kb_sync/sync.py` (flags → env → `pulumi stack output` resolution,
      `--dry-run`/`--no-ingest`/`--no-wait`). The CLI **refuses** a `--source`
      that is not a built corpus, and warns when `build/kb` is older than the
      docs or graph sources it was built from. 11 unit tests (moto S3 + fake
      bedrock-agent), mypy/ruff clean.
- [x] Retrieval carries the grades: `RetrievedPassage` exposes `doc_title` /
      `section` / `max_evidence` / `verification` / `citation_count` /
      `safety_critical` from the sidecars, plus a `.citation` string; optional
      `retrieve(min_evidence=…)` pushes an evidence floor into the vector store.
      Defaults to unfiltered — the caller decides.
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

## Phase 5b — Headline deliverable: S3 Vectors vs Aurora pgvector benchmark

**No longer "proposed" — it is the architecture.** Two Bedrock KBs over one
source bucket, differing only in the STORAGE slot. `retrieval.py:70` already
takes `knowledge_base_id`, so switching between them is config, not code.

Not "I built a RAG pipeline on Bedrock" — instead: **"I benchmarked S3 Vectors
against Aurora pgvector on the same corpus and golden set — here's retrieval
latency, recall, cost, and where each wins."** The measured decision *is* the
artifact.

- [ ] Both KBs live over the same `corpus/` prefix; identical Titan v2 @ 1024
- [ ] Golden set: ~30–50 `question → expected-passage(s)` pairs from the corpus
- [ ] Measure per store: retrieval **latency** (p50/p95, incl. Aurora cold-start
      after auto-pause), **recall@k / MRR** on the golden set, **$ cost** (at rest
      + per 1k queries)
- [ ] **The graph only Aurora can produce: sweep `hnsw.ef_search`** (10 / 40 /
      100 / 200 — a session-level `SET`, no rebuild) and plot recall@4 against
      p95 latency. S3 Vectors can only ever be one opaque point on that curve.
      This is the strongest single exhibit in the whole project.
- [ ] Also cheap and only possible in Aurora: near-duplicate detection across the
      corpus (`WHERE a.embedding <=> b.embedding < 0.15`) — answers "are my 383
      chunks actually distinct?", a question the Retrieve API cannot express.
- [ ] Writeup: a results table + "where each wins" — the README's real headline

**Verified prices (AWS Price List API, us-east-1, 2026-08-13)** so the cost
column is not guesswork: Serverless v2 **$0.12/ACU-Hr**, storage **$0.10/GB-Mo**,
backup **$0.021/GB-Mo**, I/O **$0.20 per million**. Note `min_capacity = 0` bills
**~$0.12/mo, not $0** — ACU billing pauses, storage and backup do not.

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
