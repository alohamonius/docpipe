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
- **Could not verify live AWS state during this pass.** The working shell's
  credentials returned `UnrecognizedClientException`, and the installed `aws`
  CLI is v1 — it has no `bedrock-agent` command, so `get-data-source` and
  `list-ingestion-jobs` are unavailable from it. `.venv/bin/python` has a boto3
  new enough for both. Recorded so the next session does not repeat the loop:
  refresh credentials first, and drive Bedrock control-plane calls through
  boto3, not the system `aws`.
