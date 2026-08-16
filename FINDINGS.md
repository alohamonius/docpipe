# Findings

Dated entries, newest last. Staging ground — once a cluster stabilises, promote
it into real rules (README, PLAN, a test).

## 2026-08-13 — Bedrock access day: model + platform facts

- **Bedrock Agents "Classic" is closed to new customers** (maintenance mode).
  This account got Bedrock in Aug 2026 → we cannot build on it. AWS points new
  customers at **Bedrock AgentCore** instead. Evidence:
  <https://docs.aws.amazon.com/bedrock/latest/userguide/agents-supported.html>
  ("no longer open to new customers … explore Amazon Bedrock AgentCore").
  Consequence: "managed agent" for docpipe means AgentCore (or an agent
  framework like Strands running in our own Lambda), not classic Agents.
- **DeepSeek V3.2 on Bedrock does tool use via Converse.** Smoke test
  (us-east-1, the docpipe account): `converse(modelId="deepseek.v3.2",
  toolConfig=…)` → `stopReason=tool_use`, sensible `search_kb` call, 400 total
  tokens. It is `ON_DEMAND` — invoked by plain model id `deepseek.v3.2`, **no
  inference profile** (unlike R1, which needs `us.deepseek.r1-v1:0`).
  `list_inference_profiles` shows no V3.2 profile — that's expected, not a gap.
- **DeepSeek R1 on Bedrock has no tool use** — fine for plain RAG/summaries,
  rules it out as an agent orchestrator.

## 2026-08-13 — Pulumi S3-backend + dynamic-provider gotchas

Hit while bringing the dev stack up for the first time (all fixed in
`pulumi/components/`):

- **`pulumi login s3://bucket` 301s unless the URL is region-qualified.**
  Working form: `pulumi login
  "s3://docpipe-tfstate-<ACCOUNT_ID>/pulumi?region=us-east-1&awssdk=v2&profile=docpipe"`.
- **Dynamic providers do NOT inherit `aws:profile`** — that config only reaches
  pulumi-aws. Raw boto3 in a dynamic provider falls back to the default
  credential chain (here: expired creds → `AccessDeniedException: security
  token invalid`). Fix: pass the profile in as a resource prop and build
  `boto3.Session(profile_name=…)` (see `s3vectors.py` / `invocation_logging.py`).
- **Dynamic-provider props JSON-roundtrip through the engine: ints arrive as
  floats.** `dimension: 1024` came out `1024.0` and boto3 rejected it
  (`Invalid type for parameter dimension`). Cast at the call site:
  `int(props["dimension"])`.
- **A failed `create` leaves real AWS orphans Pulumi doesn't know about.** The
  vector bucket was created, then `create_index` raised → next `up` hit
  `ConflictException`. Fix: make `create` idempotent — tolerate
  ConflictException on the bucket and adopt it (names are deterministic and
  stack-owned, `delete()` owns cleanup).

Net result: `pulumi up` → 53 resources, dev stack fully up (KB `SJQAFQXPH7`,
guardrail `pjpeeu9hf68a`, invocation logging active).

## 2026-08-13 — the corpus was already built, and we were about to ingest it wrong

Reviewing health.studio's KB before wiring Phase 3. Every item below was found by
inspection, not by a failed ingestion — nothing had been synced yet, which is the
only reason none of it caused harm.

- **The corpus is not `docs/`. It is `build/kb/`.** `pnpm kb:build`
  (`apps/web/scripts/build-kb.ts`) emits **285 chunk-final files + 285
  `.metadata.json` sidecars** — 192 prose chunks from 20 source docs, 93 from the
  body graph, ~697 KB. PLAN.md called the location "TBD"; it had been answered
  upstream for days. `build/` is gitignored (`health.studio/.gitignore:4`), so
  the artifact is local-only and CI has no corpus at all.
- **A `docs/**/*.md` glob uploads five files the corpus policy forbids.** Measured
  by running `corpusDecision()`'s rules over `rglob("*.md")`: 25 files found, 20
  include, **5 exclude (75 KB)** — `PM.md` (36 KB, self-declared uncited market
  research), `positioning-kit.md`, `LICENSE.md`, `FINDINGS.md`, and
  `anatomy/referral-boundary.md`. The last is excluded for a **safety** reason,
  not a tidiness one: to forbid a dangerous sentence the document must quote it,
  and a retrieved fragment cannot tell the quotation from the claim
  (`apps/web/src/lib/kb/corpus.ts:16-38`). Our own CLI's documented example was
  `--source ../health.studio/docs`.
- **A chunk-final corpus requires `chunkingStrategy: NONE`.** We had `FIXED_SIZE`
  (500 tokens / 20% overlap). Every built chunk opens with a header carrying the
  ★ scale and the "not a diagnosis" disclaimer; any splitter — `FIXED_SIZE`,
  `SEMANTIC`, `HIERARCHICAL` alike — strips that header from every fragment after
  the first, leaving the model bare star ratings with no scale attached.
  **`SEMANTIC` is not a fix for this.** It is a better splitter, and the problem
  is splitting at all.
- **Changing `chunkingConfiguration` REPLACES the data source.** `pulumi preview
  --diff`: `+-1 to replace, 52 unchanged`, and `dataSourceId` goes
  `KPAQK6MQY4 => [unknown]`. So the id is not stable across this change — anything
  holding `KB_DATA_SOURCE_ID` in env or `.env` points at a dead data source after
  the next `up`, and a full re-ingestion follows.
- **`rglob("*.md")` silently drops the sidecars** — they end in `.json`. Ingesting
  without them costs retrieval-time evidence filtering *and* named citations, and
  fails silently: you get a working KB whose every citation is an opaque S3 URI.
- **The corpus is honest about its own weak spots, which is exactly why retrieval
  must not be.** MANIFEST: **99/285 chunks at `maxEvidence: 0`, 180/285 with zero
  citations.** Framing prose and conceptual models are legitimate content, but
  they retrieve identically to a systematic review unless something separates
  them. health.studio's code graph already enforces a floor of 1 per-edge
  (`docs/anatomy/00-index.md:12-14`).

Sizing checks, for the record — both fit, neither with much room:

- Largest chunk is `graph/body-graph-referral-source--pancreas.md` at **24.7 KB /
  3,694 words ≈ 5k tokens**, under Titan v2's 8,192-token limit. With `NONE`,
  one file = one vector, so a graph chunk that grows past ~8k tokens breaks
  ingestion rather than degrading.
- S3 Vectors caps **filterable metadata at 2 KB per vector** (40 KB total, 50
  keys, 10 non-filterable). Sidecars are ≤1,705 B as pretty-printed JSON and
  carry 9 attributes; the stored payload is smaller, but this has **not** been
  confirmed against a real ingestion yet.

## 2026-08-13 — the managed layer moved under us

Found while researching `docs/interview/`. Both changes shipped **before** the
corresponding docpipe decisions were made, and neither was known when PLAN.md's
decisions were locked earlier today. Neither invalidates a decision; both turn a
default into a choice that now has to be argued.

- **Bedrock Managed Knowledge Base went GA 2026-06-17** (AWS NY Summit): a fully
  managed RAG service collapsing six components — ingestion connectors,
  multimodal parsing, chunking, embeddings, vector store, re-ranking — into one
  API primitive, with six native connectors (S3, SharePoint, Confluence, Google
  Drive, OneDrive, Web Crawler), hybrid search, document ranking, and agentic
  retrieval for multi-hop queries. Evidence:
  <https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-managed-knowledge-base/>
  **Consequence:** `pulumi/components/kb.py` builds the *custom* KB (own S3
  Vectors index, own chunking config) that Managed KB is designed to replace,
  and "build a RAG pipeline on Bedrock" now defaults to Managed KB. The custom
  KB is still correct **here** for one specific reason: the corpus is
  chunk-final, its chunk boundaries are the product, and Managed KB owns exactly
  the chunking/parsing/embedding layers that must not touch it (see the
  `chunkingStrategy: NONE` entry above). That reason is corpus-specific — for
  ordinary documents Managed KB would be the right call.

- **Bedrock AgentCore Harness went GA 2026-06-18**: `CreateHarness` +
  `InvokeHarness` wrap Runtime / Memory / Gateway / Identity / Browser /
  Observability as managed configuration; omitting memory provisions managed
  memory automatically; **one CLI command exports a harness as Strands-based
  code**. Evidence:
  <https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-harness-is-now-generally-available-go-from-idea-to-production-grade-agent-in-minutes/>
  **Consequence:** PLAN.md justifies "Agent = Strands SDK in the API Lambda"
  partly by Agents Classic being closed. Still true, no longer the whole story —
  Harness is a live third option alongside AgentCore Runtime. The export path
  means the choice is not a one-way door in either direction, which is the
  actual argument for keeping Strands-in-Lambda.

- **Prompt caching does not cover DeepSeek.** Supported models are Claude and
  Nova as of ~Jan 2026 (Nova 2 pending). Source is AWS cost-optimization
  material plus secondary coverage — **re-check the supported-model list before
  relying on this.** Consequence if it holds: `ChatClient.reply` re-sends the
  grounded system prompt (base + grounding instructions + k passages) as fresh
  input tokens on every turn, and the model choice forfeits the discount that
  would have covered the static preamble. Not measured; the passages change per
  query, so caching would only ever hit the preamble, not the bulk.

## 2026-08-13 — Aurora cold-resume accepted as a UX problem, not a cost problem

Decision taken today: **we want Aurora**, and the resume latency is accepted
rather than designed around. Recording the reasoning because the cost story in
`README.md` was written on the opposite assumption and now needs to move.

- **The ~15s is a cluster resume, not network latency.** Aurora Serverless v2
  with `min_capacity = 0` (`pulumi/components/data.py:156`, default
  `aurora_min_acu: float = 0`) auto-pauses after an inactivity window; the next
  connection resumes it before serving. AWS's published figure is **~15 seconds**
  from 0 ACU, with auto-pause after ~5 min idle (configurable). **Neither number
  is measured in this account** — `README.md:177` has carried a TODO for exactly
  this since Phase 2. Requires PostgreSQL ≥ 16.3 (`aurora_engine_version` default
  is 16.6, so we qualify).
- **Accepted mitigation: pre-warm on chat-UI open, not on message send.** The
  user types for 10–30s before submitting, which covers a 15s resume. This is a
  health.studio-side change (fire a warm-up when the assistant panel mounts).
- **Consequence the README does not yet reflect:** pre-warming on session open
  keeps the cluster awake through the working day. Idle cost stops being ~$0 and
  lands somewhere between $0 and the ~$43/mo warm-cluster figure in
  `README.md:169`, proportional to traffic. The "foundation costs ~$0 at rest"
  claim (`README.md:150`) survives only while Aurora stays gated off.
- **Still open at the time of writing: which role Aurora plays.** Vector store
  for the KB (replacing S3 Vectors), summary history for the Phase 5 worker, or
  both sides of the PLAN.md:161 benchmark. The resume tradeoff above is
  identical in all three; the cost and blast radius are not.
- **Unverified, and it gates the vector-store reading:** Bedrock KB + Aurora
  reaches the cluster via the RDS Data API at *query* time, so a paused cluster
  puts the resume on the user-facing retrieval path — not just the async worker.
  Measure before committing: `enable_aurora=true`, time a cold query, tear down.

## 2026-08-13 — three retrieval levers we provision nothing for

Found by introspecting the live API shapes, not from docs. All three are
supported by the account today and used by none of our code.

- **`cohere.rerank-v3-5:0` is ON_DEMAND in us-east-1**, and
  `KnowledgeBaseVectorSearchConfiguration` carries a `rerankingConfiguration`
  (`type: BEDROCK_RERANKING_MODEL`, `numberOfRerankedResults`, plus metadata
  selection). This is a query-time fix for chunk-size dilution — it re-scores
  retrieved passages against the query — and needs **no re-ingestion**.
- **`overrideSearchType: ['HYBRID', 'SEMANTIC']`** exists on the same shape.
  **Unverified whether S3 Vectors supports HYBRID at all** — hybrid needs a
  keyword index alongside the vector one, and that may be an OpenSearch-only
  capability. Check before planning on it.
- **`implicitFilterConfiguration`** takes a `modelArn` + attribute descriptions
  and lets a model derive metadata filters from the query text. Our sidecars
  already ship the attributes it would filter on (`maxEvidence`,
  `citationCount`, `safetyCritical` — see `retrieval.py:30-36`).

## 2026-08-13 — the corpus size variance is one-sided, and it is the graph half

Measured over `health.studio/build/kb` (n=383, `*.md`, whitespace word count):

```
all:    min 107  p25 215  median 354  p75 652  p90 1705  p99 2581  max 3692  (σ=647)
prose:  n=192  median 264  max  664     ← 6× spread
graph:  n=191  median 652  max 3692     ← 34× spread
>2000 words: 19 chunks (5.0%), all graph
```

Under `chunkingStrategy: NONE` one file is one 1024-dim vector, so a
3,692-word graph node (`graph/body-graph-referral-source--pancreas.md`) gets
the same representational budget as a 107-word overview. The long tail is not a
corpus-quality problem — those nodes are correct — it is a *retrieval* problem:
a specific question competes against everything else the node knows.

Fix belongs upstream in health.studio's `build-kb.ts` (split large graph nodes
on internal headings, as it already does for prose — prose chunks carry a
`part N/M` header). Not fixable by any Bedrock chunking strategy, because the
answer is not "split differently", it is "emit smaller nodes".

## 2026-08-13 — Bedrock KB rejects Cohere Embed v4, under every storage type

Measured, not read. `CreateKnowledgeBase` called with a deliberately bogus
storage target so nothing could be created, varying only the embedding model.
If the model is accepted, validation proceeds and fails on storage; if it is
rejected, the model error comes back instead. Six calls, all failed, zero
resources created (`list_knowledge_bases` verified after).

| embedding model | S3_VECTORS storage | RDS storage |
|---|---|---|
| `amazon.titan-embed-text-v2:0` | storage error → **model OK** | storage error → **model OK** |
| `cohere.embed-english-v3`      | storage error → **model OK** | storage error → **model OK** |
| `cohere.embed-v4:0`            | **rejected** | **rejected** |

Exact message, identical under both storage types:

```
ValidationException: Knowledge Base type does not support cohere.embed-v4:0 embedding model
```

- **Being invokable ≠ being usable by KB.** `cohere.embed-v4:0` is ON_DEMAND in
  this account and `get_foundation_model` returns it happily. Bedrock KB accepts
  a *subset*, and nothing in the model list marks the difference.
- **Consequence: Titan v2 stays, and it is now forced rather than chosen.** The
  only other supported family is Cohere Embed v3, whose input caps at 512
  tokens — against a corpus at p90 ≈ 1,705 words (~2,200 tokens). Cohere v3
  would truncate most of the graph half. Titan v2's 8,192 is the only fit.
- Retract the plan to switch: PLAN/earlier discussion treated Cohere v4 as a
  live option. It is not, and no `pulumi up` should carry it.

## 2026-08-13 — Aurora at `min_capacity = 0` IS supported by Bedrock KB

**Corrects an earlier caution in this file.** The worry that Bedrock KB + Aurora
would force min ACU > 0 (because retrieval hits the cluster via the RDS Data API)
is **wrong**. AWS documents ACU 0–16 for Serverless v2 as a KB vector store, with
scale-to-zero explicitly supported. The ~15s resume remains real; the *blocker*
does not exist.

Real requirements, from the Aurora user guide — more than we had assumed:

- PostgreSQL **16.1+** (our `aurora_engine_version` default 16.6 qualifies).
- **pgvector ≥ 0.5.0** — `CREATE EXTENSION IF NOT EXISTS vector;`
- **RDS Data API must be enabled.** `pulumi/components/data.py` does **not** set
  this today (`enable_http_endpoint`) — a concrete gap before Aurora can back a KB.
- A dedicated `bedrock_user` role + its own Secrets Manager secret. Our cluster
  uses `manage_master_user_password=True`, which is the *master* secret, not this.
- An exact schema Bedrock writes into, plus **three** indexes:

```sql
CREATE SCHEMA bedrock_integration;
CREATE TABLE bedrock_integration.bedrock_kb (
  id uuid PRIMARY KEY, embedding vector(1024), chunks text,
  metadata json, custom_metadata jsonb);
CREATE INDEX ON bedrock_integration.bedrock_kb
  USING hnsw (embedding vector_cosine_ops) WITH (ef_construction=256);
CREATE INDEX ON bedrock_integration.bedrock_kb USING gin (to_tsvector('simple', chunks));
CREATE INDEX ON bedrock_integration.bedrock_kb USING gin (custom_metadata);
```

- **That `to_tsvector` GIN index answers the open HYBRID question.** Aurora
  provisions a full-text index alongside the vector one, which is what hybrid
  (keyword + semantic) search needs. Whether S3 Vectors supports HYBRID at all
  is still unverified — but Aurora demonstrably does.
- Sidecar attributes land in **`custom_metadata jsonb`**, GIN-indexed — so
  `maxEvidence` / `citationCount` filtering works, and is also directly
  queryable in SQL.

## 2026-08-13 — an untracked Managed KB is live in the account

`list_knowledge_bases` returns **two**, not one:

```
SJQAFQXPH7  docpipe-dev-kb                    ← ours, Pulumi-managed
XHWRWMWMIQ  knowledge-base-quick-start-nxl5n  ← NOT in Pulumi state
```

`XHWRWMWMIQ` is `type: MANAGED` / `embeddingModelType: MANAGED` — a **Bedrock
Managed Knowledge Base** (the GA'd service noted above), created **2026-08-04**
via console Quick Create, with a service-role
`AmazonBedrockExecutionRoleForKnowledgeBase_nxl5n`. It predates the Pulumi stack
(Phase 2, 2026-08-13) and `make infra-down` will not remove it.

`describe_db_clusters` returns **0 clusters**, so no Aurora is running and the
Quick Create flow left no database behind. Decide whether to keep it as a
Managed-KB comparison point or delete it — but it should not stay unowned.

## 2026-08-14 — the corpus grew a generation, and the chunking rule became a test

Two things, both measured against `build/kb/` from a clean `pnpm kb:build` on
2026-08-14.

- **The corpus is 383 chunks, not 285.** The 2026-08-13 entry above measured
  285 (192 prose + 93 graph); that reading was right on the day. `healthstudio-kb-coverage`
  resolving rows since then took the graph half from 93 to **191**, so the shape
  is now **383 files + 383 sidecars, 1,686,867 B**. The evidence figures moved
  with it: **97/383 at `maxEvidence: 0`** (was 99/285) and **172/383 with zero
  citations** (was 180/285) — both *proportions* improved, 35%→25% unrated and
  63%→45% uncited, while the absolute counts barely moved.
- **Corpus statistics quoted in prose go stale in about a day.** `retrieval.py`'s
  module docstring and `kb_sync.py`'s sidecar comment were both a corpus
  generation behind within 24 hours of being written. `MANIFEST.json` carries
  `totals`, `evidenceHistogram` and `chunksWithNoCitation` and is regenerated by
  every build — quote it, or recount before citing a number, and never re-derive
  one from memory.
- **`chunkingStrategy: NONE` is now asserted, not commented**
  (`packages/core/tests/test_kb_chunking_contract.py`). It was written **red
  first** and observed red against the pre-fix commit `a6d4891` — all four cases
  failed, `chunking_strategy is 'FIXED_SIZE', must be 'NONE'` /
  `splitter configuration present: ['fixed_size_chunking_configuration']` /
  `data source references splitter strategies: ['FIXED_SIZE']` /
  `chunking knobs still reachable from the constructor: ['chunk_max_tokens',
  'chunk_overlap_percentage']` — then green on `dbc3e8d`. A test that was never
  red proves nothing.
- **The check reads the AST, not the deployed stack, and that is deliberate.**
  `pulumi-aws` lives in `pulumi/venv`, not the uv workspace, so a Pulumi-mock
  test would make the contract depend on an optional install — the same
  "enforced by nothing, in practice" failure it exists to end. Parsing also
  ignores comments, which is required here: the component's comment names
  `FIXED_SIZE`, `SEMANTIC` and `HIERARCHICAL` as the strategies it refuses, so a
  `grep`-based check would fire on its own rationale. It does **not** assert what
  is live in AWS — a console edit to the data source would pass. Detecting that
  needs a `describe_data_source` check against the deployed stack, which is not
  written.

## 2026-08-14 — the app stays on Hetzner, and the VPC is not the seam

Asked whether the whole system could move onto AWS, since running the Next.js
app on Hetzner and the AI backend on AWS felt like an arbitrary split. It can.
It should not, and the reason is arithmetic.

- **AWS compute is ~3–5× Hetzner for the same box.** The app is `flowforge` on
  **taro** (shared Hetzner CAX11 ARM, port 3333 → 3000, alongside starogram :80
  and themeltemi :8080/:8443 — see `~/git-public/hustling/INFRA.md`). Its
  marginal cost is a fraction of one €4–7 box. The nearest EC2 equivalent
  (`t4g.small`/`t4g.medium` + EBS) is ~$15–27/mo, and **moving off taro would
  not retire taro** — two other tenants stay. Pure added spend, not a swap.
- **Joining a rented box to the VPC is possible, and the managed forms are
  priced for someone else.** List prices, us-east-1, verify before committing:
  Site-to-Site VPN ~$0.05/hr ≈ **$36/mo** per connection plus data; a Transit
  Gateway attachment the same; a Client VPN endpoint ~$0.10/hr ≈ $73/mo. Each
  costs several times the server it would connect. The versions that fit the
  budget are **WireGuard on a `t4g.nano` in the VPC (~$3–4/mo)** or a
  **Tailscale subnet router (free tier)**.
- **None of it is needed, because the VPC is not the seam.** Everything docpipe
  exposes — API Gateway, Bedrock, DynamoDB, S3, SQS — is a public endpoint
  behind IAM auth. The only genuinely private tenant is Aurora, and the app does
  not talk to Aurora: **Bedrock** does. The app's own Postgres is its own DB.
  So the integration is **HTTPS + SigV4 to API Gateway** — no tunnel, no NAT
  (docpipe deliberately has none, ~$33/mo), $0 of networking.
- **The seam does not exist yet.** `pulumi/__main__.py` wires network, data,
  messaging, kb, iam, safety — 53 resources, and **no Lambda and no API
  Gateway**; the only Lambda artifact is an exported role ARN. So this is a
  choice about a thing not yet built, not a migration.
- **The one real cost of the split is latency, and AWS is not its fix.**
  us-east-1 is Northern Virginia; taro is in the EU, so every retrieval and
  generate call pays ~90–110ms each way. A VPN makes that *worse* — same path,
  plus IPsec. The fix at Hetzner prices is **Hetzner's Ashburn location**, same
  metro as us-east-1, ~1–5ms. Caveat: `CAX` (Ampere ARM) is EU-only, so a US box
  is x86. The app's `Dockerfile` is `node:22-alpine` with **no platform pin**,
  and neither `docker-compose.prod.yml` nor `.github/workflows/deploy.yml` pins
  an architecture, so it rebuilds for amd64 unchanged.

**Decision (2026-08-14, the human's):** the app stays on Hetzner. A second
Hetzner server comes later rather than an EC2 host. Do not reopen "move the app
to AWS" without new numbers — and note that the latency argument is answered by
a Hetzner region change, not by a cloud change.

**Explicit non-decision:** this is about *hosting*, not about model access. The
app calls the Anthropic API directly while docpipe uses Bedrock. That split is
untouched here and should be decided on its own merits.

## 2026-08-14 — the guardrail was decoration, and GLM lost on grounds other than the numbers

- **A provisioned Bedrock guardrail enforces nothing.** `safety.py` created one
  and computed `self.guardrail_arn`; **nothing read it** — not exported in
  `__main__.py`, not referenced in `iam.py`, and `llm.py::_converse` passed no
  `guardrailConfig`. So the non-diagnostic stance rested entirely on a system
  prompt, which prompt injection can argue with. `scripts/status.py:181` already
  instrumented this (`guardrail_enforced()` greps the source for the parameter)
  and was correctly reporting **false**. Now wired: `guardrailConfig` on every
  Converse call, `bedrock:ApplyGuardrail` on both roles, and the flag reads true.
- **The permission and the parameter must ship together.** Adding
  `guardrailConfig` without `bedrock:ApplyGuardrail` fails every chat call with
  AccessDenied; adding the permission without the parameter changes nothing.
  Half of this diff is inert without the other half — which is why it is one
  commit and not two.
- **An intervention was indistinguishable from an answer.** Bedrock replaces the
  model's output with the guardrail's blocked message and returns
  `stopReason: "guardrail_intervened"` — readable text, HTTP 200. Without
  surfacing it, a blocked reply and a real one look identical to the caller and
  the intervention rate is unmeasurable. `ChatReply.guardrail_intervened` now
  carries it.
- **`foundation-model/deepseek*` granted every future DeepSeek model**, including
  ones nobody has evaluated. Replaced with the two we actually invoke. Also split
  `bedrockModelId` into chat + summary config: one value fed both policies, so
  the `InvokeChatModel` statement carried the **summariser's** inference-profile
  ARN and worked only because the wildcard happened to cover the chat model.
  Correct by accident is not correct.
- **GLM is real, cheap, and was rejected anyway.** Measured 2026-08-14 in-account,
  us-east-1: `zai.glm-5`, `zai.glm-4.7`, `zai.glm-4.7-flash` all `ON_DEMAND`, all
  three returned `stopReason=tool_use` on a one-tool Converse probe. Price List
  API, standard on-demand $/1M in-out: **glm-4.7-flash 0.07 / 0.40**, glm-4.7
  0.60 / 2.20, **deepseek.v3.2 0.62 / 1.85** — flash is ~9× cheaper on input,
  ~4.6× on output. `zai.glm-5` is invocable but has **zero rows in the us-east-1
  price list**, so it cannot be budgeted. The ruling was one model family over a
  cheaper second one; recorded here because the measurement stays true whichever
  way the decision went, and re-measuring costs another hour.
- **Lambda bills the model's thinking time, and it barely matters.** Per 1M chat
  turns at ~3,000 in / 400 out and 8s on 512 MB: Lambda duration ≈ **$67**, HTTP
  API ≈ $1, REST API ≈ $3.50 — against ≈ **$2,600** of `deepseek.v3.2` tokens.
  Think-time is ~2.5% of the bill; **input tokens are ~70%**, so `top_k` is a
  larger cost lever than anything in the compute layer. The binding constraint is
  not cost but the API Gateway integration timeout (HTTP API caps at 30s, not
  raisable) and the fact that neither API Gateway integration streams — a
  token-by-token UI needs a Lambda Function URL with `RESPONSE_STREAM`.

## 2026-08-14 — Aurora has a fifth gap, and the docs disagreed about whether it exists

Triggered by a plain question — "why was Aurora rejected?" — whose premise was
wrong. Aurora was never rejected; it is `PLAN.md`'s stated primary vector store.
Reading the code to answer properly turned up a gap the plan did not list.

- **The Aurora component produces no outputs, so nothing can consume it.**
  `pulumi/components/data.py:142-171` creates the cluster and its instance into
  a *local* `cluster` variable: no `self.aurora_*` attributes are set, and
  `register_outputs({})` is empty. Bedrock KB's `rdsConfiguration` needs
  `resourceArn`, `credentialsSecretArn`, `databaseName`, `tableName` and a
  `fieldMapping`; `pulumi/__main__.py` can read **none** of them, and
  `components/kb.py` never mentions Aurora at all. This is prior to the four
  gaps recorded on 2026-08-13 (`enable_http_endpoint`, `bedrock_user`, the
  table + three indexes, and — also newly listed — the second KB resource
  itself): fix the wiring first or the other four have nowhere to plug in.
- **"Gated behind a flag" read as "one flip away", and it is not.**
  `Pulumi.dev.yaml` said only that Aurora "stays off". A reader — including me,
  earlier in this session — infers the cluster is otherwise ready. Five items
  stand between `enableAurora: "true"` and a KB that attaches. The flag now
  carries that warning inline.
- **Three documents, three statuses, for one decision.** `PLAN.md:43` called
  Aurora pgvector primary (a locked decision); `Pulumi.dev.yaml:7` called it a
  Phase 5b item that stays off; `README.md` claimed the foundation costs "~$0 at
  rest", which is true *only* while Aurora is off. None of the three was
  false in isolation; together they described three different projects. This is
  the same failure as the chunking contract — **a decision written in prose, in
  more than one place, reconciled by nothing.** The chunking case was closed by
  making the contract a test. There is no equivalent for "is Aurora built", and
  the honest fix here was narrower: state build status next to every claim about
  it, and date-stamp claims about live AWS state so a stale reading is visible
  as stale rather than read as current.
- **A stale blocker outlived its blocker.** `PLAN.md`'s pre-flight still said
  the `NONE` chunking change was "uncommitted and unapplied" two commits after
  it was committed (`dbc3e8d`) and tested (`5430984`). *Committed*, *tested* and
  *deployed* are three separate claims and the plan now states each separately —
  the AST-based contract test deliberately does not check AWS, so nothing in CI
  can collapse them for us.
- **Could not verify live AWS state during this pass.** ~~The working shell's
  credentials returned `UnrecognizedClientException`~~ — **RETRACTED 2026-08-14,
  same session.** The credentials were fine; the call was wrong. boto3 was
  invoked with no profile, so it fell through to the ambient chain instead of
  the `docpipe` profile the stack uses (`Pulumi.dev.yaml:5`). `AWS_PROFILE=docpipe`
  and every call succeeded. **`UnrecognizedClientException` here meant "wrong
  identity", not "expired credentials"** — and the reflex it triggered,
  `aws sso login --profile docpipe`, was wrong twice over: `docpipe` is a static
  IAM user key, not an SSO profile, so there is no login to refresh. Two
  minutes were spent on a nonexistent auth problem, and a stale reading was
  nearly committed as current because of it.
  **The part that stands:** the installed `aws` CLI is v1 and has no
  `bedrock-agent` command, so `get-data-source` / `list-ingestion-jobs` are
  unavailable from it; `.venv/bin/python` has a boto3 new enough for both.
  Next session: `AWS_PROFILE=docpipe .venv/bin/python`, and read the error
  before believing it.

**Live state, measured 2026-08-14** (`AWS_PROFILE=docpipe .venv/bin/python`,
boto3, us-east-1). This is the reading the entry above nearly failed to take:

| | |
|---|---|
| KB `SJQAFQXPH7` | `ACTIVE`, storage `S3_VECTORS` |
| Embedding | `amazon.titan-embed-text-v2:0`, `dimensions: 1024`, `FLOAT32` |
| Data source `KPAQK6MQY4` | `AVAILABLE`, updated 2026-08-12 23:02 UTC |
| Chunking **(live)** | `FIXED_SIZE`, `maxTokens: 500`, `overlapPercentage: 20` |
| Ingestion jobs | **0** |
| Source bucket `docpipe-dev-kb-source-<account-id>` | **0 objects** |

Three things this settles. The KB's embedding config **matches the plan
exactly** — Titan v2 @ 1024 FLOAT32 — so that decision needs no re-verification.
The `NONE` chunking fix is **still unapplied**: committed `dbc3e8d`, asserted
`5430984`, and the deployed data source has not moved since 2026-08-12, which is
the clearest possible demonstration of why the AST test cannot stand in for a
`describe_data_source` check. And **nothing is ingested** — 0 objects, 0 jobs —
so the pre-flight's premise holds and every corpus-shape decision is still free.

## 2026-08-14 — the bibliography fix is unshippable as planned, and it is not urgent

Correcting `f1a85e3` (*"the oversized-chunk fix is not a split, it is the
bibliography"*) and the PLAN.md step-1 it wrote. The **measurement** in that
commit stands and is good; the **plan built on it** has two blockers it did not
find, and the urgency it claimed is not real. Read against both trees rather
than re-derived, at the request of how2doo's `kb-retrieval-readiness` set, which
owns this objective as goal `04-embedding-payload`.

**What survives.** By-section over the 132 `body-graph-connection--*` docs:
`Sources` 39,717w (301/doc) is the single largest section in the corpus, ahead
of Key clinical connections 38,110w and Anatomy 36,621w. Excluding it from the
embedded body, modelled over all 191 graph chunks: p50 652→557, p90 1,994→1,358,
max 3,692→2,402, `>1k` 86→70, `>2k` **19→2**, with zero files renamed. The
producer-side mechanism is real: `health.studio` `stamp.ts:144-168`
(`metadataFor`) already emits every attribute as `{ value, includeForEmbedding }`
and `MetadataValue` already has `STRING_LIST` (`stamp.ts:21`); the six `Sources`
emitters are at `graph.ts:220` (connection groups), `:257` (chains), `:359`
(nerves), `:439` (entrapments), `:513` (joints), `:577` (referral sources).

**BLOCKER A — nothing on this side would deliver it.**
`packages/core/src/docpipe_core/retrieval.py:39-52` defines `RetrievedPassage`
with a **fixed** list of seven sidecar fields and no `sources`; `_passage_of`
(`:101-114`) never reads one, and `citation` (`:54-64`) never renders one. The
plan says "docpipe's `RetrievedPassage` will parse it" — future tense, other
repo, unowned. Ship the health.studio half alone and the bibliography is stored
and never returned: the citation trail disappears from what the model and the
reader receive. That is the same bare-evidence failure the `NONE` chunking rule
exists to prevent, arriving through the sidecar instead of through a splitter.

**BLOCKER B — `includeForEmbedding: false` does not buy non-filterable.**
`pulumi/components/kb.py:80-89` constructs `S3VectorsIndex` with **no
`metadataConfiguration` block at all**, so no key is declared non-filterable and
the 2 KB filterable cap applies to every attribute. The largest bibliography is
~1,832w ≈ 12 KB — it cannot fit. `includeForEmbedding` and filterability are
different axes and only the second one has that cap; `f1a85e3` filed the cap as
"unverified and it blocks" but read it as a measurement to take after the change,
when it is a config precondition before it. Non-filterable keys are declared at
index creation, so this is a Pulumi index change (expect a replace), not the
"cross-repo; no AWS" the plan promised. *Not yet verified:* that the key set is
immutable post-creation. The absence of the declaration is read from the file;
the immutability is not — confirm against the API before relying on it.

**The urgency was misattributed.** "Free right now, stops being free after the
first ingest" is the **rename** hazard — and this change renames nothing, as the
commit itself says. An orphan comes from a changed *key*, not changed *content*:
S3 overwrites a re-PUT key in place and Bedrock re-embeds it. There is no
expiring window here. The reverse is true and was missed: landing the reshape
before the first ingest means there is no pre-change baseline to score it
against, so the quality claim ("a real retrieval win") becomes permanently
unfalsifiable.

**Ruling — human, 2026-08-14: ingest as-is first.** PLAN.md's execution order
inverts. `pulumi up` → first ingest → golden set scored on the **current**
corpus as the control → *then* the bibliography move, with a signed delta
against that control. The corpus reshape is no longer step 1.

## 2026-08-16 — Pre-ingest review: the corpus is clean, the destination is not

Review of the data staged for the first ingest (`health.studio/build/kb`, 383
chunks). Every number here was measured today against the built corpus and the
**live** dev stack; nothing is quoted from an earlier report.

**The corpus itself is ready.** Measured over all 383 documents + 383 sidecars:

| check | result |
|---|---|
| sidecar JSON schema (`metadataAttributes`, typed values) | 0 errors, 383/383 |
| attribute keys | **10**, uniform on every chunk, one type each — *not 9, as `docs/kb-data-status.md` and this file's 2026-08-14 entry both say* |
| orphan sidecars / docs missing a sidecar | 0 / 0 |
| S3 key hygiene (charset, case collisions, length) | clean; longest key 123 B |
| largest chunk vs Titan v2's 8,192-token ceiling | 6,158 tok (`referral-source--pancreas`), 75% of ceiling; 0 over, 1 over 6k |
| whole corpus | 418,604 tokens (cl100k proxy) — confirms the "~420k, ≈1¢ to re-embed" figure |
| custom metadata payload per vector | median 326 B, **max 413 B** |
| non-`.md` files that would be swept into the bucket | 1 (`MANIFEST.json`) — excluded, `discover_corpus` globs `*.md` only |

Command: `python3 .scratch/kb_audit.py ../health.studio/build/kb` (gitignored —
promote it to `scripts/` if this should run in the gate rather than by hand).

**The 2 KB filterable cap was compared against the wrong object.** This file
(2026-08-14, "Sizing checks") measures the *sidecar* against the 2 KB filterable
metadata limit and finds it fits at ≤1,705 B. The sidecar is not what the cap
bites. Bedrock stores the **chunk body itself** in the vector as
`AMAZON_BEDROCK_TEXT`, and with no `metadataConfiguration` on the index that key
is filterable like any other. Verified live today:

```
$ aws s3vectors get-index --vector-bucket-name docpipe-dev-vectors \
      --index-name docpipe-dev-kb          # via boto3 1.43.62; the CLI on PATH predates s3vectors
{"dataType":"float32","dimension":1024,"distanceMetric":"cosine",
 "encryptionConfiguration":{"sseType":"AES256"}}   ← no metadataConfiguration
```

`243 of 383 chunks are larger than 2,048 bytes` (p50 2,504 B, p90 11,348 B, max
24,747 B). So **BLOCKER B is a step-2 blocker, not a step-4 one**: it does not
wait for the bibliography move, it lands on the very first ingest of the corpus
as it stands — the ingest PLAN.md's corrected order makes step 2. *Predicted, not
yet measured in this account:* the practitioner report matching this config is a
`ValidationException` at ingestion time
(<https://dev.to/aws-heroes/data-ingestion-rss-feeds-knowledge-base-s3-vectors-and-metadata-filtering-4n8m>,
who fixed it by declaring **both** `AMAZON_BEDROCK_TEXT` *and*
`AMAZON_BEDROCK_METADATA` non-filterable). One ~1¢ ingest of a single >2 KB file
settles it; do that before trusting either direction.

**The two blockers mask each other, so fixing one alone is worse than fixing
neither.** The live data source still reports the wrong chunker —
`get_data_source(KPAQK6MQY4)` → `FIXED_SIZE, maxTokens 500, overlap 20%`, 0
ingestion jobs, confirming `dbc3e8d` is still unapplied. FIXED_SIZE at 500 tokens
produces fragments mostly under 2 KB, which is why the filterable cap has never
been hit. Apply `NONE` on its own and every chunk arrives whole: the safety
defect (evidence legend stripped from every fragment after the first) is traded
for a hard ingestion failure across ~63% of the corpus. **Both fixes must land in
the same `pulumi up`.**

**Non-filterable will not rescue the bibliography move either — the Bedrock
integration caps *custom* metadata at 1 KB.** The 2026-08-14 entry concludes
BLOCKER B is "a Pulumi index change (expect a replace)", which implies that
declaring `sources` non-filterable lets ~12 KB of bibliography fit under the
40 KB total. It does not. Verbatim, from the S3 user guide's Bedrock integration
page: *"When using S3 Vectors as your vector store with Amazon Bedrock Knowledge
Bases, you can attach up to 1KB of custom metadata and 35 metadata keys per
vector."*
(<https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-bedrock-kb.html>,
Limitations). Measured headroom against that 1 KB: 413 B used, ~600 B free —
about 150 words of citations, against a largest bibliography of 1,832 words.
**PLAN.md step 4 is not implementable on S3 Vectors in its current design**,
independent of the index fix. It survives on the Aurora/pgvector KB, which makes
the bibliography move a *reason to build the second KB* rather than something to
do first — or it needs a different carrier (a docId-keyed lookup resolved at
citation time, outside the vector).

**Index metadata config is immutable — confirmed, closing the 2026-08-14
"*not yet verified*".** The S3 Vectors API (botocore 1.43.62) exposes
`CreateIndex`, `DeleteIndex`, `GetIndex`, `ListIndexes` — and **no `UpdateIndex`**;
`metadataConfiguration.nonFilterableMetadataKeys` is a `CreateIndex`-only input.
Fixing B is therefore delete-and-recreate. Free today (0 vectors, 0 jobs), a full
re-ingest later.

```
$ uv run --with boto3 python -c "import boto3; print(sorted(boto3.client('s3vectors',
    region_name='us-east-1').meta.service_model.operation_names))"
['CreateIndex','CreateVectorBucket','DeleteIndex','DeleteVectorBucket',…,'PutVectors','QueryVectors',…]
```

**Trap in the way of that fix:** `pulumi/components/s3vectors.py:19` lists the
replace-triggering inputs in `_KEYS`, and the provider defines no `update()`.
Adding `non_filterable_metadata_keys` to the resource's props **without** adding
it to `_KEYS` makes `diff()` return `changes=False` — Pulumi reports "unchanged"
and the index keeps its old (empty) config. The fix must touch both.

**Near-duplicate detection, run for the first time** — this was open question 6
in `docs/kb-data-status.md` ("cannot be asked through the retrieval API"). It can
be asked of the corpus offline, and the answer is *the corpus is not diluted by
duplication*. Normalised bodies (generated header stripped, URL targets removed),
5-gram shingle Jaccard over all 73,153 pairs:

```
exact duplicate bodies        0
pairs ≥ 0.9 / 0.8 / 0.7 / 0.6 0 / 0 / 0 / 0
pairs ≥ 0.5                   44, involving 19 chunks
```

Every one of the 44 is a `body-graph-structure--*` pair (fibula↔tibia 0.58,
navicular↔talus 0.56, the eight carpals ~0.55). That is the same 31-chunk cohort
already indicted as item 5 — a rendered ★ with no source behind it. So three
independent measurements now converge on the same cohort: template-shaped, no
citations, `verification: ["NONE"]`, and (below) the worst signal-to-boilerplate
ratio in the corpus. They are the corpus's weakest asset, not a random tail.

**Boilerplate is 27.5% of the median chunk, and the majority of 51 of them.**
Every chunk carries the same generated header — provenance comment, title,
`Source`, the four-level evidence scale, the "not a diagnosis" line. Measured to
the first `---`: median **27.5%** of the chunk's bytes, >33% for **151** chunks,
>50% for **51**. Worst: `body-graph-structure--tibia` at 69% (646 B of 938 B),
`06-breathing-and-cervical-connection--01-overview` at 71%. With `NONE`, that
text is inside every vector, so ~28% of the average embedding budget is spent on
a string identical across all 383 chunks. This is *not* an argument to remove it
— it is there for the safety reason `NONE` exists — but it is unmeasured cost
that belongs next to the bibliography's ~20% in any embedding-payload work, and
it hits the short chunks hardest, which are exactly the ones with the least
signal to spare.

**17 citation collisions.** `RetrievedPassage.citation`
(`retrieval.py:54-66`) renders `docTitle → section (★★)`. 41 chunks share a
`(docId, section)` pair with at least one other chunk, in 17 groups — 4 chunks all
cite as *"Myofascial Chains → The 12 Myofascial Meridians"*, 4 more as
*"Muscle Anatomy Database → Upper Body"*, 4 as *"… → Lower Body"*. Two different
passages in one answer can therefore carry the identical citation string, and a
reader cannot tell which was used. Producer-side fix (a disambiguating suffix in
`section`), no infra involved.

**Unresolved — `STRING_LIST` on S3 Vectors.** All 383 sidecars carry
`verification` as `STRING_LIST`. Sources conflict on whether the Bedrock KB +
S3 Vectors path accepts it: the practitioner article above lists STRING_LIST
among the four supported KB types, while an AWS re:Post thread reports it working
on pgvector and **not** on S3 Vectors (403 to automated fetch; read it by hand
before relying on either). Cheap insurance: `verification` is the one attribute
no filter reads — `retrieval.py` only ever filters on `maxEvidence` and
`safetyCritical` — so if ingestion rejects it, flattening it to a comma-joined
`STRING` costs nothing downstream. Worth knowing before the first ingest, since
the failure would hit all 383 documents, not a subset.

### Addendum, same day — three corrections to the entry above

Written after the fixes landed in code and `pulumi preview` ran. Left in place
rather than edited in, because two of them correct claims I made in this file.

1. **The Knowledge Base is replaced too, not just the data source.** The entry
   above says fixing BLOCKER B is a delete-and-recreate of the *index*. It is
   also a replace of `aws:bedrock/agentKnowledgeBase`: the KB references the
   index by ARN, and Pulumi cannot know at plan time that the new ARN is
   identical (it is — bucket and index names are deterministic), so it plans a
   replace. **`knowledgeBaseId` changes**; `SJQAFQXPH7` is dead after the apply.
   `kb-policy` shows its whole statement block as a diff for the same reason —
   it is an `Output.json_dumps` over the replaced ARN and re-renders identically.
   Nothing is lost: 0 vectors, 0 ingestion jobs. Re-read the id from
   `pulumi stack output`, never from `.env` or `status.json`.
2. **241 of 383 chunks exceed 2 KB, not 243.** The 243 was measured on the
   pre-06 corpus. `06-structure-provenance` merged as `health.studio@cc146e2`,
   which changed 162 chunk bodies and 75 `maxEvidence` values (every one a
   downgrade). Post-merge: 1,679,754 B, p50 2,470, p90 11,314, max 24,747;
   418,397 tokens; evidence histogram ★★★ 84 · ★★☆ 86 · ★☆☆ 69 · unrated 144.
   The conclusion is unchanged — 63% of the corpus still cannot be ingested with
   everything filterable — but the number in an argument should be the number.
3. **`_KEYS` fired, and the preview proves it.** The trap named above was real
   and is now closed: `pulumi preview` reports the index as `(replace)` with
   `+ non_filterable_metadata_keys` on the diff. Had the key been left out of
   `_KEYS`, this is exactly where it would have said "unchanged" instead.

The procedure these feed into is `docs/INGEST-RUNBOOK.md`, not this file — this
is where the evidence lives, that is where the sequence lives.

### 2026-08-16, pre-flight — `sync.py` has no AWS profile, and the default one is wrong

Found while answering "are we ready to ingest", by running the runbook's step-3
dry run verbatim before touching anything. It failed:

```
$ uv run python services/kb_sync/sync.py --source ../health.studio/build/kb --dry-run
  383 chunks (192 prose from 20 docs, 191 graph)      # ← corpus summary prints fine
botocore.exceptions.ClientError: An error occurred (403) when calling the HeadObject operation: Forbidden
```

`CorpusSyncer` constructs `boto3.client("s3")` and `boto3.client("bedrock-agent")`
with no profile and no region (`packages/core/src/docpipe_core/kb_sync.py:161-162`),
so it takes whatever the default credential chain gives it. On this machine that
is `[default]` in `~/.aws/credentials` — an **invalid** key
(`aws sts get-caller-identity` → `InvalidClientTokenId`) pointed at `eu-west-1`.
Wrong twice: wrong identity, wrong region.

`AWS_PROFILE=docpipe` alone is sufficient — that profile carries
`region = us-east-1`, so `AWS_REGION` is redundant. With it:

```
would upload: 766   unchanged (skipped): 0
```

which also confirms `health.studio/build/kb` is current at `cc146e2` and the 383
sidecars ride along.

**Why it was never noticed.** Every other AWS entry point already had a profile
and so could not surface this: Pulumi reads `aws:profile: docpipe` from
`Pulumi.dev.yaml`, `scripts/kb_eval.py:36` and `scripts/status.py:24` both
hardcode `PROFILE = "docpipe"`. `sync.py` is the only one that inherits, and it
had never run against a live bucket — there was nothing ingested to run it
against. A credential path with no test and no first use is not a working
credential path; it is an untested one.

**Misleading in the specific way that costs time:** the 403 lands *after* the
corpus summary has printed, so it reads as a corpus or permissions-on-the-bucket
problem rather than "you are not who you think you are".

**Fix applied:** `docs/INGEST-RUNBOOK.md` now prefixes steps 3 and 4 and carries
the symptom in its failure table. The durable fix — a `--profile` flag on
`CorpusSyncer` mirroring `kb_eval.py` — is deliberately deferred: it is an
unmeasured code change on the ingest path, and the baseline is the thing that
makes later changes falsifiable. Do it after.

### 2026-08-16, first ingest — `COMPLETE`, `failed: 0`, and zero vectors written

The first real ingestion job (`KWZPC25FGS`, KB `JDNNGSU1JT`, ds `U0PM4HIXGE`)
reported success and indexed nothing. This entry is the correction to two claims
this file and `docs/INGEST-RUNBOOK.md` made confidently, and both of them are the
reason it took a manual check to notice.

```
status: COMPLETE
numberOfDocumentsScanned         : 383
numberOfMetadataDocumentsScanned : 383
numberOfNewDocumentsIndexed      : 0
numberOfDocumentsFailed          : 0
numberOfDocumentsSkipped         : 0
failureReasons: ["Ignored 383 files as the associated metadata was larger
                  than service limit of MaximumFileSizeSupported: 1024 bytes"]

s3vectors list_vectors(docpipe-dev-vectors/docpipe-dev-kb) → 0 vectors
```

**Correction 1 — "the number that matters is `failed`" was wrong.** It was `0`,
and so were `Skipped` and `Failed`, and the job status was `COMPLETE`. Bedrock
*ignores* a document whose sidecar is oversized: it is counted in `Scanned`,
counted in no other bucket, and reported only in `failureReasons` — a field on a
job whose status is `COMPLETE`. Every summary number is consistent with success.
The only honest checks are `numberOfNewDocumentsIndexed` and an actual
`list_vectors` count, and the runbook now says so.

Had this not been checked, the next step was the baseline eval, which would have
scored 0 recall against an empty index and looked like a corpus failure.

**Correction 2 — "current usage is 413 B, so ~600 B is free" was wrong by 4×.**
Measured over all 383 sidecars: **1,547–1,705 B each, p50 1,609, and 383/383
exceed 1,024**. The 1 KB Bedrock custom-metadata cap was already documented in
the runbook — as an argument about why the *bibliography* plan is dead on S3
Vectors — while the corpus was, at that moment, already over it. The cap was
read as a ceiling to plan against instead of a gate already closed.

**Where the bytes go.** The attribute *values* total **86 B**. The other ~94% is
the typed-envelope format: each of 10 attributes carries
`{"value":{"type":…,"…Value":…},"includeForEmbedding":…}`, ~150 B of scaffolding
apiece, and `apps/web/scripts/build-kb.ts:121` writes it with
`JSON.stringify(…, null, 2)`.

**Priced fixes** (all 383, measured, `over-1KB` counts):

| variant | p50 | max | over 1 KB |
|---|---|---|---|
| as built (pretty-printed) | 1,609 | 1,705 | 383/383 |
| minify only | 1,042 | 1,134 | 349/383 |
| minify + omit `includeForEmbedding:false` | 818 | 910 | **0/383** |
| minify + flat untyped form | 333 | 425 | **0/383** |
| minify + typed, 5 keys | 540 | 616 | **0/383** |

Minifying alone is **not** enough — that is the trap, because it is the obvious
one-character fix and it clears only 34 of 383.

**Open, and blocking the choice:** whether `includeForEmbedding` defaults to
`false` when omitted. If it defaults to `true`, omitting it starts embedding
`docId`, `sourcePath` and `citationCount`, which changes retrieval quality —
under a baseline whose whole purpose is to make later changes falsifiable. Not
assumed either way here. The flat untyped form has the most headroom (600 B) but
drops `includeForEmbedding` entirely, which is the same unmeasured change made
unconditionally.

**Not settled by this job:** `STRING_LIST` on S3 Vectors. Nothing was embedded,
so the open risk recorded above is still open — the sidecars never got far
enough to be type-checked.

The fix site is `health.studio`'s `apps/web/scripts/build-kb.ts` +
`src/lib/kb/stamp.ts`; the behaviour is docpipe's ingestion path, so the finding
lives here and is referenced from there.
