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
- **Vector store = Aurora PostgreSQL + pgvector as *intended* primary; S3 Vectors
  retained as a second KB over the same source bucket.** `min_capacity = 0` **is**
  supported by Bedrock KB (ACU 0–16), so scale-to-zero survives; the ~15s resume
  is accepted and hidden by pre-warming on chat-UI open. Two KBs make Phase 5b a
  config flip (`retrieval.py` already takes `knowledge_base_id`) and double as a
  rollback path. S3 Vectors idles at ~$0, so keeping it costs nothing.
  **Build status: not built.** Only the S3 Vectors KB exists today; the dev stack
  sets `enableAurora: "false"` (`pulumi/Pulumi.dev.yaml:8`) and flipping it to
  true still would not produce a KB-attachable cluster — see the five gaps in
  Phase 2. "Primary" is the target, not the deployed state.
  **Deferred 2026-08-16 (the human's): shipping on S3 Vectors alone; Aurora
  and the benchmark move behind shipping — see Current priority.**
- **KB corpus = `health.studio/build/kb/`**, produced by `pnpm kb:build` — **383
  chunk-final files + 383 `.metadata.json` sidecars** (192 prose from 20 docs,
  191 graph; 1,686,867 B). **Not `docs/`**: that tree holds files health.studio's
  corpus policy excludes, one of them for a safety reason. Because the corpus is
  chunk-final, the data source runs `chunkingStrategy: NONE` — see FINDINGS.md.

## Current priority (set 2026-08-14, the human's)

**The measured decision is the deliverable, so Phase 5b comes first.** What this
project is judged on is *"I benchmarked S3 Vectors against Aurora pgvector on the
same corpus and golden set — here is recall, latency and cost"*, not a shipped
chat endpoint. Phase 3's agent/API work and Phase 4's health.studio integration
are **deferred behind it**. The benchmark does not need either: `retrieval.py`
calls the Retrieve API directly, so nothing on the eval path goes through a
Lambda or API Gateway.

Execution order, and what each step is actually blocked on:

1. **`pulumi up`** — apply the already-committed `NONE` chunking (`dbc3e8d`) and
   guardrail wiring (`ff6c91f`). Replaces the data source; `dataSourceId` changes.
2. **First ingest** into the S3 Vectors KB, **of the corpus as it stands.**
3. **Golden set** — 30–50 `question → expected-passage(s)` pairs, scored against
   that first ingest, which is the **control group**. Blocked on nothing except
   the corpus being final, entirely offline, and the single biggest hole in the
   project: there is no evaluation story at all today.
4. **Shrink the oversized graph chunks upstream** (health.studio) — **move
   `Sources` out of the embedded body into a non-filterable metadata sidecar
   attribute**, rather than splitting files. Sources is the single largest
   section in the corpus (39,717w across the 132 connection docs, ~301w each)
   and it is bibliography competing with clinical content for one vector.
   Dropping it from the body takes graph chunks >2,000w from **19 → 2** and
   renames **nothing** — so it carries none of the orphan-vector risk a split
   does. **This step needs AWS after all** and it needs work here, not only
   upstream: see the two blockers in FINDINGS.md 2026-08-14 (`RetrievedPassage`
   has no `sources` field, and the S3 Vectors index declares no
   `metadataConfiguration`, so nothing is non-filterable and the 2 KB cap bites).
   Land the consumer half before or with the producer half, never after.
5. **Aurora** — the five gaps in Phase 2, then a second KB over the same bucket.
6. **Measure** — recall@k / MRR / p50 / p95 / $, plus the `hnsw.ef_search` sweep.

> **Order corrected 2026-08-14 (human ruling).** Steps 1–4 were 2–4 then 1: the
> corpus reshape led, on the argument that it was "free before the first ingest".
> That is the *rename* hazard, and this change renames nothing — an orphan comes
> from a changed key, not changed content. What the old order did cost was the
> baseline: reshaping first leaves nothing to score the reshape against. Ingest
> first, measure, then reshape against a control. Tracked as
> `kb-retrieval-readiness/04-embedding-payload` in how2doo.

> **Revised 2026-08-16 (human ruling): ship on S3 Vectors.** Steps 1–3 are
> done — `NONE` chunking applied, first ingest (383 vectors), golden set +
> baseline scored: **recall@5 0.7879**, signed report at
> `docs/baselines/2026-08-16-s3vectors-baseline.json`, miss-rank probe on top.
> Steps 4–6 (bibliography move, Aurora, the benchmark) are **deferred, not
> deleted** — two KBs existed only for the benchmark, shipping needs one, and
> time is the constraint. The S3-only path forward: **reranker over a widened
> pool** (measured ceiling ~0.96 on the answerable set, no re-ingest; also the
> fix for gap-04's outranked red-flag chunk), then abstention for the
> not-covered class, then Phase 3. The bibliography move parks with Aurora —
> as designed it needs non-filterable metadata the live S3 index cannot add
> without a rebuild. conn-04/chain-03 phrasing fixes are content-side in
> health.studio, unblocked anytime.

Phases are deliberately **not renumbered**: FINDINGS.md and `docs/interview/`
reference them by number. Only the execution order changed.

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
- **Aurora gaps — five, found 2026-08-13/14. All five now written (2026-08-14),
  none yet applied.** The code exists; no AWS resource does. What remains is
  three ordered operations, not more code:
  - [x] **`enable_http_endpoint` (RDS Data API)** — set in `data.py`. This is
        the entire connection path: Bedrock reaches Aurora over the Data API,
        which is also why a cluster in private subnets needs no NAT.
  - [x] **Dedicated `bedrock_user`** — its own Secrets Manager secret, created
        empty by Pulumi (`data.py`). The master secret stays out of Bedrock's
        reach; the KB role's `GetSecretValue` is scoped to the one ARN.
        **The password is never in Pulumi state** — `aurora_bootstrap.py`
        generates it and writes version 1.
  - [x] **`bedrock_integration.bedrock_kb` + three indexes** —
        `scripts/aurora_bootstrap.py`, idempotent, over the Data API with the
        master secret. HNSW `ef_construction=256`, GIN `to_tsvector`, GIN
        `custom_metadata`. `hnsw.ef_search` stays a session knob, which is what
        makes the Phase 5b sweep possible without an index rebuild.
  - [x] **Second KB resource** — `pulumi/components/kb_aurora.py`, over the
        *same* source bucket, identical Titan v2 @ 1024 FLOAT32, identical
        `chunkingStrategy: NONE`. Held identical on purpose: if these drift the
        benchmark measures the drift instead of the store.
  - [x] **The component exports nothing** — fixed first, since the other four
        had nowhere to plug in. `data.py` now sets `aurora_cluster_arn`,
        `aurora_cluster_endpoint`, `aurora_database_name`,
        `aurora_master_secret_arn`, `aurora_bedrock_secret_arn` and
        `aurora_bedrock_username`, all exported from `__main__.py`.
- [ ] **Apply it — three ordered steps, and the order is not negotiable.**
      Bedrock validates the DB connection at `CreateKnowledgeBase`, so the table
      and role must exist before the KB is created. Hence two flags:
      1. `enableAurora: true` → `pulumi up` (cluster + Data API + empty secret)
      2. `AWS_PROFILE=docpipe python scripts/aurora_bootstrap.py`
      3. `enableAuroraKb: true` → `pulumi up` (the KB attaches)
      `__main__.py` raises if step 3 is attempted without step 1. Nothing
      enforces step 2 — that failure surfaces as a KB create error.
- [ ] **Untested against AWS.** The Aurora branch in `data.py` has **never
      executed** (`describe_db_clusters` → 0 clusters, verified 2026-08-14), so
      the first `up` runs it for the first time. `kb_aurora.py` has never been
      previewed. Treat the first apply as a test, not a deployment.
- [ ] **Untracked resource:** Managed KB `XHWRWMWMIQ`
      (`knowledge-base-quick-start-nxl5n`, created 2026-08-04 via console Quick
      Create) is live and outside Pulumi state — `make infra-down` will not
      remove it. Keep as a Managed-KB comparison point, or delete.
- [x] `messaging` module: SQS queue + DLQ + redrive policy
- [x] Remote state: self-managed S3 backend (`pulumi login s3://…?region=…`)
- [x] `make infra-preview` / `infra-up` / `infra-down` for the dev stack

## Phase 3 — API service (Lambda + API Gateway) — the agent ships here

### Pre-flight — ordered, and all of it is free only while the KB is empty

**Re-verified 2026-08-14:** the source bucket holds **0 objects** and the data
source has run **0 ingestion jobs**, so every corpus-shape decision is still
reversible at zero cost. That stops being true after the first ingest. Do these
in order:

- [ ] **BLOCKER — deployed config ≠ code.** The `NONE` change in
      `pulumi/components/kb.py` is now **committed** (`dbc3e8d`) and **asserted
      by a test** (`5430984`, written red first) — but **not applied**.
      **Re-verified 2026-08-14** against the live stack: data source
      `KPAQK6MQY4` is `AVAILABLE`, last updated 2026-08-12 23:02 UTC, and still
      reports `chunkingStrategy: FIXED_SIZE` (`maxTokens: 500`,
      `overlapPercentage: 20`). Ingesting under `FIXED_SIZE` strips the ★ legend
      and the "not a diagnosis" header off every fragment after the first.
      `pulumi up` → **then** sync. The `up` **replaces** the data source, so
      `dataSourceId` changes.
- [ ] **The chunking test does not check AWS.** `test_kb_chunking_contract.py`
      parses the component's AST, by design (FINDINGS.md, 2026-08-14) — a
      console edit to the live data source would pass it green. Closing that
      needs a `describe_data_source` check against the deployed stack, which is
      not written. Until it is, "committed" and "deployed" are separate claims
      and this file should keep stating both.
- [ ] **Shrink the oversized graph chunks upstream** (health.studio) —
      **decided 2026-08-14 (the human's): before the first ingest**, not after;
      step 1 of the current priority order. Prose is healthy (p50 265w, max
      664w); graph runs p50 652w to **max 3,692w** — one 1024-dim vector per
      file means a whole organ's referral map gets the same budget as a
      107-word overview.
      **The fix is not a split.** Measured 2026-08-14 by section: `Sources` is
      the largest section in the corpus — 39,717w across the 132
      `body-graph-connection--*` docs (~301w each, 852w in the largest) — i.e.
      labels and URLs consuming ~30% of the embedding budget. Moving it to the
      metadata sidecar with `includeForEmbedding: false` (a flag `stamp.ts`
      already has) takes graph chunks >2,000w from **19 → 2** and >1,000w from
      86 → 70, while renaming **zero** files. Splitting would rename ~86, and a
      rename after ingest strands vectors that keep retrieving as current text
      (`CorpusSyncer` never deletes). Handed to a health.studio agent.
      **Two blockers, found 2026-08-14 — see FINDINGS.md.** (A) `retrieval.py`'s
      `RetrievedPassage` has no `sources` field and `_passage_of` never reads
      one, so the producer half alone stores the bibliography and never returns
      it. (B) `kb.py` creates the S3 Vectors index with no `metadataConfiguration`,
      so **no key is non-filterable** and the 2 KB filterable cap applies to all
      of them; the largest bibliography is ~12 KB. `includeForEmbedding: false`
      does not address this — different axis. Sidecars are already ≤1,705 B, so
      there is no headroom to absorb it either. Both must land here before the
      upstream change ships. Whether the 2 remaining >2k chunks need splitting is
      a later call.
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
