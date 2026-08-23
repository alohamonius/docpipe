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
- **Vector store = two Bedrock KBs over one source bucket — Aurora PostgreSQL +
  pgvector and S3 Vectors — differing only in the STORAGE slot.**
  `min_capacity = 0` **is** supported by Bedrock KB (ACU 0–16), so scale-to-zero
  survives. Two KBs make Phase 5b a config flip (`retrieval.py` already takes
  `knowledge_base_id`) and double as a rollback path. S3 Vectors idles at ~$0,
  so keeping it costs nothing.
  **Build status: BOTH BUILT AND INGESTED (2026-08-20).** The dev stack sets
  `enableAurora: "true"` and `enableAuroraKb: "true"`
  (`infra/Pulumi.dev.yaml:17-18`). Live and verified 2026-08-21: cluster
  `docpipe-dev-aurora` **available**, engine 16.14, min 0 / max 1 ACU, Data API
  enabled; Aurora KB **`A44CISMRAM`** (ds `H8PN3JBXPN`) and S3 Vectors KB
  **`JDNNGSU1JT`** (ds `U0PM4HIXGE`), both holding the same **585**-chunk corpus
  (reingested 2026-08-22; vector counts verified directly, not trusted from a
  job status — S3 Vectors 585 vectors, Aurora 585 rows / 585 embeddings).
  The ~15s resume this entry used to assume was **measured at 1.32 s** on the
  Retrieve path (FINDINGS 2026-08-20) — pre-warming on chat-UI open is
  therefore not needed to hide it.
  **Which store is primary is now an open question, not a locked decision** —
  the benchmark it was waiting on came back with the store effect near zero
  (recall 0.7879 identical; MRR 0.5634 S3 vs 0.5646 Aurora after the
  score-inversion fix), Aurora ~2× faster warm (p50 0.523 s vs 0.960 s), and
  the $ column plus the `hnsw.ef_search` sweep still outstanding. See Phase 5b.
  **The 2026-08-16 deferral is spent: Aurora and the benchmark were built
  anyway on 2026-08-20 — see Current priority.**
- **KB corpus = `health.studio/build/kb/`**, produced by `pnpm kb:build` — **585
  chunk-final files + sidecars** (192 prose from 20 docs, 393 graph). Was 383,
  then 496 at the 2026-08-19 heading split, then 579, and **585 after the
  2026-08-21 Sources peel** — four entrapment chunks had grown past Titan v2's
  8,192-token cap and were producing NO vector at all; `### Sources` was peeled
  onto siblings with the parent keys kept, so nothing was retired. `radiocarpal`
  still sits at 8,172 of 8,192 — the build now warns at 90%. Both KBs hold the
  byte-identical 585-chunk corpus. **78 are bibliography-only**,
  tagged `section: "Sources"` in every sidecar, so retrieval can exclude them
  with a metadata filter today — no rebuild needed. The corpus change measured
  recall- and rank-neutral on the ratified key (0.7879 either way).
  **Not `docs/`**: that tree holds files health.studio's corpus policy
  excludes, one of them for a safety reason. Because the corpus is chunk-final,
  the data source runs `chunkingStrategy: NONE` — see FINDINGS.md.

## Current priority (set 2026-08-14, the human's) — ⚠️ SUPERSEDED, see the end of this section

> **Read the 2026-08-20/21 block at the bottom first.** Steps 1–3, 5 and most
> of 6 are done; Aurora is built and ingested and the benchmark has reported.
> The dated rulings below are kept because they record *why* the order was what
> it was — they are history, not the current instruction.

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
   ✅ **done 2026-08-20** — all five closed, three applies run, KB `A44CISMRAM`
   ACTIVE, 496/496 ingested (585/585 since 2026-08-22).
6. **Measure** — recall@k / MRR / p50 / p95 / $, plus the `hnsw.ef_search` sweep.
   ◐ **mostly done 2026-08-20** — recall, MRR and warm p50/p95 measured on both
   stores, single-shot and agentic. **Still open: the $ column and the
   `ef_search` sweep** (which needs a direct-SQL harness — Bedrock issues its
   own sessions, so the sweep cannot reach the shipped Retrieve path).

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

> **Overtaken by events 2026-08-20/21.** The deferral above did not hold: steps
> 5 and 6 ran anyway. Aurora was built and ingested (496/496) on 2026-08-20 and
> the benchmark produced the headline numbers — so the ordering question it
> settled is moot, and this section no longer describes what is being worked on.
>
> **What the benchmark actually returned is the reason to re-plan rather than
> resume:** the store barely matters. Recall is identical across stores
> (0.7879), MRR within noise (0.5634 S3 / 0.5646 Aurora, and only after finding
> that Bedrock's RDS path returns cosine *distance* as score sorted
> worst-first), and every agentic row lands 0.89–0.95 on either store in any
> search mode. **The retrieval loop is the effect; the store is not.** Aurora's
> one clear win is warm latency (~2×) and the exhibits only it can produce
> (`ef_search` sweep, near-duplicate detection).
>
> **Therefore the remaining Phase 5b work is not "more benchmark".** What is
> left that changes an outcome: the **$ cost column**, the **`ef_search`
> sweep** (needs a direct-SQL harness), and the measured run that FINDINGS'
> six-run entry pointed at — **verbatim query as tool call 1, then
> reformulate**, since single-shot ∪ agentic = 61/61 answerable but neither
> strategy alone reaches it. Then Phase 3, where the agent ships.
>
> Still genuinely open and unaffected by any of this: the bibliography move
> (step 4) and the S3-side reranker/abstention path from the 2026-08-16 ruling.

> **Revised 2026-08-19 (human ruling): Aurora is must-have — unparked.** The
> 2026-08-16 deferral inverts: the three Aurora applies, `search_type` in
> `retrieval.py`, sync fan-out to both KBs, and the hybrid/benchmark
> measurement rejoin the active path. The accumulated case (hybrid, the
> bibliography move, chatCompacts-as-memory, rerank-quota relief, cost
> re-estimate) is inventoried in FINDINGS.md 2026-08-19. Same ruling: worker
> compute leans **Lambda** (not locked); the nerve-registry rulings (cutaneous
> contract, Population B denominator) and coverage priority are resolved in the
> platform repo, not here. The sources-chunks handling decision stays open.

> **2026-08-23 — Phase 5b close-out.** Everything the 2026-08-20/21 block left
> open is now measured: the **$ column** (store is 1–3% of marginal cost), the
> **`ef_search` sweep** (flat at n=585), the **near-dup scan** (304 pairs, all
> graph registry), the **guarded re-baseline + null controls** (guardrail
> −10 points, stochastic, on the clinical strata) and the **verbatim-first
> run** (+4 points, 0.9467). All under Phase 5b below with FINDINGS entries.
> What the close-out surfaced as new critical-path work: **the DRAFT guardrail
> blocks the corpus's own question shapes ~⅓ of the time — tune it before
> Phase 3 ships.** Only the writeup remains.

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

Originally built as Terraform modules in `infra/`, then ported to **Pulumi
(Python)** (one `ComponentResource` per concern + two boto3 dynamic providers
for pulumi-aws gaps: S3 Vectors, invocation logging). Terraform deleted in
`825aa4b`; the Pulumi tree lived in `pulumi/` until it reclaimed the `infra/`
name on 2026-08-21 — so `infra/` before that date means Terraform.
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
  - [x] **Second KB resource** — `infra/components/kb_aurora.py`, over the
        *same* source bucket, identical Titan v2 @ 1024 FLOAT32, identical
        `chunkingStrategy: NONE`. Held identical on purpose: if these drift the
        benchmark measures the drift instead of the store.
  - [x] **The component exports nothing** — fixed first, since the other four
        had nowhere to plug in. `data.py` now sets `aurora_cluster_arn`,
        `aurora_cluster_endpoint`, `aurora_database_name`,
        `aurora_master_secret_arn`, `aurora_bedrock_secret_arn` and
        `aurora_bedrock_username`, all exported from `__main__.py`.
- [x] **Apply it — three ordered steps, and the order is not negotiable.**
      Bedrock validates the DB connection at `CreateKnowledgeBase`, so the table
      and role must exist before the KB is created. Hence two flags:
      1. `enableAurora: true` → `pulumi up` (cluster + Data API + empty secret)
      2. `AWS_PROFILE=docpipe python scripts/aurora_bootstrap.py`
      3. `enableAuroraKb: true` → `pulumi up` (the KB attaches)
      `__main__.py` raises if step 3 is attempted without step 1. Nothing
      enforces step 2 — that failure surfaces as a KB create error.
      **Done 2026-08-20**: cluster `docpipe-dev-aurora` (16.14, min 0 / max 1
      ACU), KB **`A44CISMRAM`** ACTIVE, ds `H8PN3JBXPN`.
- [x] **Untested against AWS** — tested 2026-08-20, and the prediction held:
      four first-run bugs (non-ASCII SG description, retired 16.6 pin, Data API
      multistatement, missing `rds:DescribeDBClusters` on the KB role), one per
      apply, all fixed in code. FINDINGS.md 2026-08-20 has the evidence and the
      state-surgery playbook.
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
      `infra/components/kb.py` is now **committed** (`dbc3e8d`) and **asserted
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
      needs no rebuild. Billed per query, not per token. Rate **verified
      2026-08-23**: $0.002/search unit (1 query ≤ 100 docs) — the Price List
      API now carries the SKU (`Cohere Rerank v3.5`), and the pricing page
      agrees; the earlier "no rerank SKU" note is retracted in FINDINGS.
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

## Phase 4b — Guided assessment: the agent that runs a protocol, not a Q&A

**The product question, asked 2026-08-23: can the agent *do* things — walk the
user through physical tests and hand back exercises — rather than only answer?**

**The content already exists, graded and cited.** This is not corpus work:

| doc | what it provides |
|---|---|
| `03-finding-your-compensations.md` | Tier 1 Mirror ★★☆, Tier 2 Movement Speed ★★☆, Tier 3 Cross-Body ★☆☆, Tier 4 Breathing ★★☆ — plus *Important Caveats About Self-Assessment* |
| `04-neuromuscular-reeducation.md` | Practical Techniques, Progression Framework, Measuring Progress, *When Re-Education Isn't Enough* |
| `15-medical-red-flags.md` | the screening gate, with a machine-readable summary table |
| `evidence-honesty.md` | the downgrade rule — the machinery for presenting a ★☆☆ finding honestly |

### The constraint is NOT the guardrail

The instinct is that `safety.py` blocks this. Read again, it likely does not:
`MedicalDiagnosis` denies *"identifying, confirming, or ruling out a disease,
condition, or injury for an individual based on their symptoms"*. "One shoulder
sits lower, which is consistent with upper-trap dominance" is not that, and
exercises are not "medications, drugs, or specific medical treatments".
**Assume it passes, then verify against the live guardrail before building on
the assumption** — that prediction is exactly the kind this repo has been wrong
about before.

### The constraint the corpus names about itself

Two findings already on record make the real argument:

- **scapular dyskinesis is a normal finding, and the examiner knowing about the
  pain makes it commoner** (health.studio FINDINGS 2026-08-18)
- `12-pain-science.md` — fear-avoidance, catastrophizing, DIMs/SIMs:
  **telling someone their body is asymmetric can make their pain worse**

So the hazard is not the agent saying something forbidden. It is the agent
doing exactly what it was asked — running tests, finding an asymmetry,
reporting it — where **the reporting is the harm**. Everyone has asymmetries; a
system built to hunt for them will always find some. A ★☆☆ Tier 3 finding
presented as "here is what is wrong with you" is worse than no finding at all.

That is a design constraint, not a filter to bolt on afterwards.

### Blockers

- [x] **Guardrail on the agentic path** — `KbAgent` sent no `guardrailConfig`
      at all until 2026-08-23. Cleared; the loop is guarded on every iteration
      and reports `AgentSession.guardrail_intervened`.
- [ ] **Multi-turn state.** `KbAgent.ask()` is one question → one answer. An
      assessment is inherently stateful: propose test → user reports what they
      saw → interpret → next test or exercise. That is a different object, not
      a longer prompt.
- [ ] **More than one tool.** There is exactly one, `search_kb`. This needs at
      minimum a red-flag screen and some notion of recording a finding.
- [ ] **An instrument.** Every number here is recall@k on 66 questions. "Did it
      run a good assessment" is not that, and without a new instrument this
      ships unmeasured — which is precisely how graph expansion came to be
      built on a single anecdote (FINDINGS 2026-08-23). Candidate assertions:
      the red-flag screen ran first and was not bypassable; no finding was
      presented above its evidence grade; no pattern was named that the user
      did not report.
- [x] **Verify the guardrail actually permits the flow** — verified 2026-08-23,
      and the answer is **no, not reliably**: on the ratified question set the
      DRAFT guardrail intervenes on a 31-question pool (~20–23 per run,
      stochastic — zero questions blocked in all six runs), covering 4/4
      nerve, 3/3 refer, 2/2 entrapment questions, and blocks some *before any
      retrieval* — including `prose-02`, whose corpus answer is the
      pain-science reassurance content. Symptom-shaped first-person questions
      are what `MedicalDiagnosis` pattern-matches, and test-interpretation
      language lives in the same space. **The blocker inverted: this phase
      needs the guardrail tuned, not verified** — and so does Phase 3.
      (FINDINGS 2026-08-23 re-baseline entry.)

### Open questions — decide before writing code

- **Does the assistant ever name a pattern** ("upper-trap dominant"), or only
  reflect back what the user reported? Naming is more useful and is the step
  that walks toward diagnosis. This is the single highest-leverage decision in
  the phase.
- **Who owns the red-flag gate** — the agent, or health.studio? Phase 4 already
  lists an existing red-flag-gate component in the app. A gate in the app is
  harder to prompt-inject past; a gate in the agent travels with every caller.
- **What does a finding persist as?** An opaque session, or something the user
  accumulates over weeks (`Creating Your Personal Compensation Map` implies the
  latter). That is a privacy decision as much as a product one — Phase 4
  requires opaque conversation IDs and no PII.
- **Re-measurement as the honest exit:** *Measuring Progress* in `04` means the
  same test re-run later. That is the only claim of benefit the system can make
  that is not a promise.

### Smallest honest first version

Red-flag screen, non-bypassable → **one** Tier 1 test (★★☆, observation-only)
→ user reports what they saw → **one** exercise from `04` with its evidence
grade shown → progress = re-run the same test.

One test, one exercise, one loop. It exercises every hard part — state,
multi-tool, the gate, evidence-grade presentation — without committing to a
protocol tree, and it gives the instrument something to score.

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

- [x] Both KBs live over the same `corpus/` prefix; identical Titan v2 @ 1024
      — live 2026-08-20; both verified holding the 585-chunk corpus 2026-08-22
- [x] Golden set: the ratified 66-question key (v2, 2026-08-17) serves this
- [x] **recall@k / MRR per store — measured 2026-08-20** (FINDINGS entries,
      reports in `docs/baselines/2026-08-20-*`): recall 0.7879 identical on
      both; MRR — S3 Vectors 0.5634, Aurora semantic 0.5646 *after* the
      score-inversion fix (Bedrock's RDS path returns cosine distance as
      score, sorted worst-first — found, fixed client-side via
      `semantic_score_is_distance`, worth an AWS case), Aurora HYBRID 0.5331
      (net negative vs healthy semantic on this key); S3 Vectors rejects
      HYBRID outright. **Warm latency measured too: Aurora p50 0.523 s /
      p95 0.873 s ≈ 2× faster than S3 Vectors 0.960 / 1.797.** Cold-start
      sample remains; the **$ column filled 2026-08-23**
      (`docs/baselines/2026-08-23-cost-column.md` — verdict: the store is
      1–3% of marginal cost; DeepSeek dominates at ~$0.006/question; rerank
      is the priciest retrieval component at $0.002/query; Aurora's real
      cost is the ~$0.005 cold-visit wake, not the warm query).
- [x] **The `hnsw.ef_search` sweep — RUN 2026-08-23, and it is a flat line.**
      (`scripts/aurora_sql_eval.py`, direct SQL via the RDS Data API, one
      transaction per value so `SET LOCAL` holds.) recall@4/5/10, MRR and
      per-question ranks are **identical at ef 10/40/100/200** — at n=585 the
      ef=10 beam already reaches the whole graph, so there is no
      approximation error to trade. "The strongest single exhibit" exists only
      at corpus sizes where HNSW actually approximates; the honest chart is
      the flat line, labeled with why. The salvage: same store/corpus/
      questions, direct SQL p50 0.154 s vs Bedrock Retrieve 0.523 s — ~70% of
      the shipped Aurora path's latency is Bedrock, not Postgres. Denominator
      caveat when citing: sweep recall@5 0.8033 is answerable-only (61);
      × 61/66 = 0.7424, the shipped-path number exactly. FINDINGS 2026-08-23;
      `docs/baselines/2026-08-23-aurora-585-efsearch-sweep.json`.
- [x] Near-duplicate detection — **RUN 2026-08-23: 304 pairs < 0.15**, and the
      structure is the finding: prose is clean (every prose pair is two
      sections of the same authored doc; zero cross-document pairs), while the
      graph registry supplies the mass — ~93 sibling-nerve pairs (closest
      0.0133, proper vs common palmar digital median), and `--sources` chunks
      colliding with their own doc's content sections. Measured confusability
      exactly where the `nerve-*` misses live; feeds the bibliography-move
      case. FINDINGS 2026-08-23;
      `docs/baselines/2026-08-23-aurora-585-near-dupes.json`.
- [ ] **Retrieval-strategy matrix (added 2026-08-20)** — one corpus, one
      ratified key, every strategy a row. Single-shot rows are done (above);
      agentic rows run the Converse tool loop (`scripts/kb_agentic_eval.py`,
      V3.2, model-driven reformulation, ≤5 iterations) and score
      **session_recall** — deliberately not MRR, a multi-query session has no
      single ranked list, so the two report shapes must not be compared as if
      they measured the same thing:
      - [x] S3 Vectors single-shot · [x] Aurora single-shot (score-fixed) ·
        [x] Aurora HYBRID single-shot
      - [x] S3 Vectors agentic · [x] Aurora agentic · [x] Aurora HYBRID
        agentic — two runs each, 2026-08-20: all land 0.89–0.95
        session_recall, store- and mode-agnostic; single-shot ∪ agentic
        = 61/61 (FINDINGS six-run entry). ⚠️ Those runs were **unguarded**
        (the eval never passed a guardrail id — FINDINGS 2026-08-23).
      - [x] **Re-baseline at 585 with the guardrail + null controls +
        verbatim-first — 14 runs, 2026-08-23** (FINDINGS entry): corpus
        496→585 costs nothing (0.9057 vs 0.9098 unguarded); the **guardrail
        costs −10 points** (0.8033 guarded), stochastically — 31-question
        block pool, zero always-blocked, nerve 4/4 / refer 3/3 / entr 2/2
        ever-blocked; the **verbatim seed buys +4 back** (0.9467, three of
        four runs at exactly 58/61; union 60/61, `conn-05` the one answerable
        question nothing agentic ever hits). Trade-off: the seed zeroes
        abstention on the not-covered class. Guardrail tuning is now on the
        critical path before Phase 3 ships.
      - [x] **rerank rows — measured 2026-08-22/23, both stores.** recall@5
        **0.7879**, MRR **0.6571**, identical on S3 Vectors and Aurora — and
        identical question by question (66/66 same hit, 66/66 same rank, 63/66
        identical top-5). The cross-encoder demotes vector search to a
        candidate generator for one 25-doc pool, so every property that
        distinguishes the backends lives *below* it. **On the shipped path,
        Aurora vs S3 Vectors is not a retrieval-quality decision.**
        The sweep must be **paced, not retried**: Cohere Rerank 3.5 is 3
        req/min at ACCOUNT level and `Adjustable: false`, so ~22 min per store
        is a floor set by AWS, and both stores share one bucket — they cannot
        run in parallel.
      - [x] **(a) corpus-native graph expansion — BUILT 2026-08-21**
        (`docpipe_core.graph`, `make graph-index`, `graph expand` in the
        playground). The body-graph chunks are a typed, per-edge-graded graph
        already: six id-declaring fields address 301 of 304 chunks, and four
        sections yield **247 edges** (`in_chain` 84, `has_member` 84,
        `innervated_by` 56, `supplies` 23; 224 graded, 56 `[VERIFIED]`).
        Whole index = **106 KB of JSON** — no store, no LLM, no AWS resource.
        **SCORED 2026-08-23, and the verdict is negative: dominated by `k=9`.**
        Against the nine genuine misses of the reranked 585 baseline, graph
        expansion (budget 4) rescues **2**; plain `k=9` rescues **6**, and the
        graph's two are a strict subset of the six. Expansion spends up to four
        extra passages, so `k=5+4` and `k=9` are the same context budget — at
        that budget the graph loses. Three of the nine produce **zero hops**,
        because their only relationship content is the prose sections that
        carry no parseable ids, so it is thinnest exactly where retrieval is
        weakest.
        The A/B that justified building it — *"vector search never surfaced
        either fascial-line chunk; expansion added both"* — ran at `top_k=4`.
        That is evidence the right chunks sat just outside a narrow window,
        i.e. an argument for widening `k`, read as an argument for traversal
        because traversal was what was being built. **A demo showing "X was
        missing and my feature added it" is not evidence until the null control
        has been run.**
        Not retracted, scoped: this is retrieval *reach* on a mostly
        single-hop question set, and says nothing about answer quality or the
        agentic path. `chain-05` — which rerank loses and traversal recovers —
        shows the edge does know something the cross-encoder does not.
        **Do not ship as a default.** If it is to be defended, the test is a
        question set that *requires* two hops; if it does not win there, it
        does not win.
      - [ ] **(b) managed Bedrock GraphRAG on Neptune Analytics — priced
        2026-08-21, and the ruling is: not now.** Price List API, us-east-1:
        **$0.03 per m-NCU-hour**, smallest tier 16 m-NCU = **$0.48/hr ≈
        $350/mo**, and a *stopped* graph still bills 10% (≈$35/mo) — there is
        no scale-to-zero, unlike Aurora's ~$0.12/mo. Affordable only as
        create→measure→destroy (~$1.50 for a three-hour session). Two further
        blockers found while pricing it: graph construction supports only
        Claude 3 Haiku / Haiku 4.5 / Nova, which **breaks the DeepSeek-only
        decision**; and GraphRAG *infers its own graph* from the prose via
        `CHUNK_ENTITY_EXTRACTION`, discarding the hand-verified edges rung (a)
        traverses. Worth doing eventually as the third row — *no graph /
        verified graph / inferred graph* — but it measures a different
        question, and rung (a) answers ours.
- [x] **Playground ("tester window"), asked for 2026-08-21 — built same day**
      (`scripts/playground.py` + `.html`, `make playground`). Verified live
      against both KBs: single-shot and agentic, S3 Vectors and Aurora,
      HYBRID, rerank, multi-turn history. Two asymmetries it surfaces rather
      than hides — the agentic path applies **no guardrail** and ignores
      `rerank` (see FINDINGS 2026-08-21). Original spec: a local page
      (thin script over `docpipe_core`, same pattern as the eval CLIs) — one
      question box; toggles for store, single-shot vs agentic, HYBRID,
      rerank, `min_evidence`; output shows passages with citations + scores,
      the agent's tool-call trace, latency and token cost. Local because the
      AWS credentials live here; once Phase 3's API exists it can point there
      instead.
- [x] Writeup — **landed 2026-08-23** as README "The benchmark" section:
      answerable-denominator results table (single-shot / rerank / agentic
      guarded–unguarded–verbatim), the three punchlines (the store is not the
      lever; the guardrail gives back what the loop buys; verbatim seed +4),
      and the where-each-wins table with the introspection verdict.

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
- [ ] `make infra-down` clean-pass verified (no orphaned resources)

## Non-goals

- Multi-env promotion (only `dev`), custom domains, auth beyond API keys,
  multi-region. Noted in README as conscious scope cuts.
