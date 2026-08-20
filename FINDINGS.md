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

## 2026-08-14 — the syncer could not delete, and the bucket is a measured zero

`CorpusSyncer` uploaded and skipped; it had no `delete_object` and no
remote-minus-local diff. Its own docstring leaned on Bedrock re-embedding
"added/changed/**deleted**" documents — true of Bedrock, false here, because
Bedrock's *deleted* means the object left the bucket and nothing ever took one
out. Chunk keys are slugified section headings, so a heading rewrite in
health.studio's `docs/` mints a new key and left the old one embedded and
retrievable **forever**. Two chunks then answer the same question, both carrying
a valid stamped header, and retrieval cannot tell which one is stale. Ordinary
editing was enough to trigger it; no reshape required.

**Pre-fix census — the bucket has NEVER been ingested.** Measured, not assumed,
2026-08-14 against `docpipe-dev-kb-source-733866507398` with the `docpipe`
profile:

```
aws s3 ls s3://…/ --recursive --summarize   → Total Objects: 0
aws s3api list-object-versions --bucket …   → no versions, no delete markers
local build/kb (clean pnpm kb:build)        → 383 documents + 383 sidecars
```

Versioning is **Enabled** on the bucket and has been since creation
(2026-08-12T22:55:52Z), so anything ever PUT and later removed would still show
as a noncurrent version or a delete marker. Zero of both is therefore positive
evidence of "nothing has ever been here", not merely "nothing is here now" —
which is the distinction the goal asked for. **Orphan count: 0, of the
never-ingested kind.** Consequence: goal 02's human gate (step 6, "the human
sees the orphan list before it goes") does not open — there is nothing to
delete. The delete path was exercised against moto only.

**Guard: one ratio, not three mechanisms.** The three candidates were
refuse-when-local-is-empty, refuse-above-an-N%-deletion-ratio, and
dry-run-by-default. Shipped the ratio (`max_delete_ratio`, default **10%**):

- it *subsumes* the empty-tree guard (an empty local tree is a 100% deletion) and
  additionally catches the realistic failure the empty check misses — a
  **half-built** `build/kb`, or a `--source` pointing one directory too deep;
- dry-run-by-default was rejected because `--dry-run` **already exists** on the
  CLI. Making it the default duplicates an existing affordance, and it degrades
  the safe half (upload) to guard the dangerous half (delete);
- 10% is measured against the 2026-08-14 corpus, not picked. Largest family a
  routine edit can retire: `body-graph-structure`, 31 chunks = 8.1%. Every prose
  source-doc family is ≤ 15 chunks = 3.9%. The 132-chunk
  `body-graph-connection` family is 34% and is *meant* to trip the guard —
  retiring a third of the corpus is a decision, not a side effect. Asserted in
  `test_default_ratio_admits_the_biggest_routine_rename`.

The guard refuses **before any mutation** — no upload, no delete, no ingestion
job. A tree that cannot be trusted to delete cannot be trusted to upload either.

**Deletion order is a safety property.** Document first, then its sidecar. A
`.md` left without its `.metadata.json` is a *silent* defect: Bedrock re-embeds
it with no attributes, so an unrated chunk sails through a `min_evidence` filter
in `retrieval.py`. The reverse — a sidecar with no document — is inert, nothing
reads it. So a prune that dies half-way must die on the inert side. This is also
why `_prune` calls `delete_object` per key rather than the 1000-key
`delete_objects` batch: S3 gives no ordering guarantee within a batch.

Red-first, on this branch: the rename test failed on HEAD with
`AssertionError: assert 'corpus/anatomy/00-…--01-overview.md' not in [… it was
still there …]` — 11 failed / 12 passed. After the fix, 68/68 repo-wide.

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

### 2026-08-16, same day — the ingest landed, and `STRING_LIST` is settled

Job `UAPFGFPWDZ`, after shrinking the sidecars in `health.studio`
(`stamp.ts` drops `docId` + `collection`; `build-kb.ts` writes minified):

```
numberOfDocumentsScanned    : 383
numberOfNewDocumentsIndexed : 383
numberOfDocumentsFailed     : 0
failureReasons              : []

s3vectors list_vectors → 383 vectors        # the check that matters
sidecar budget: largest 917 B (107 B headroom)
```

**`STRING_LIST` is supported on S3 Vectors.** The open risk recorded above is
closed by measurement, not by argument: a live vector carries
`verification: ["VERIFIED"]`. The re:Post thread reporting otherwise is wrong,
or was fixed. No flattening in `stamp.ts` is needed, and the runbook's
`failed: 383` row can be struck.

**Chunking `NONE` confirmed end-to-end.** `AMAZON_BEDROCK_TEXT` on a live vector
is 4,968 chars — a whole chunk, evidence legend intact, not a 500-token fragment.

**Filtering survived the non-filterable declaration.** The thing that could have
been silently traded away was not:

| query | hits | `maxEvidence` values returned |
|---|---|---|
| unfiltered | 5 | 3, 3, **0**, 3, 3 |
| `maxEvidence >= 2` | 5 | 3, 3, 3, 3, **2** |
| `safetyCritical = true` | 5 | 1, 1, 3, 1, 1 |

**`NUMBER` comes back as a float.** `maxEvidence` is `3.0`, not `3` — S3 Vectors
round-trips Bedrock's NUMBER type as a float. `retrieval.py::_as_int` already
handles it (`isinstance(value, float) and value.is_integer()`), so nothing
breaks; noted because a stricter `isinstance(v, int)` would have silently
disabled every evidence filter while looking like it worked.

**Which attributes are safe to drop, and which only look safe.** `docId` and
`collection` are read by nothing — not `retrieval.py`, not `kb-census.ts`.
`kind` looks identical from `retrieval.py`'s side (it never reads it) and is
**not** droppable: `kb-census.ts:205` groups the census by it. Check both
consumers, not one.

**The durable guard is `assertSidecarBudget`** in `health.studio`'s
`build-kb.ts`: the build now fails if any sidecar exceeds 1,024 B, naming the
worst offenders. Headroom is 107 B — roughly one long document title — so the
gate, not the fix, is what keeps this from recurring. It converts a silent
total loss discovered three steps downstream into a local build error.

## 2026-08-16 — the baseline: 0.7879, and the exit code is the corpus's known defect being measured, not a bug

First scored run against the live KB (`JDNNGSU1JT`, job `UAPFGFPWDZ`'s 383
vectors, ratified key, k=5, `min_evidence: null`). The full signed report is
committed at `docs/baselines/2026-08-16-s3vectors-baseline.json` — the control
every later change is scored against. Headline:

```
recall@5 0.7879 · MRR 0.5538 · stamp integrity 1.0 (330/330)
prose 1.0 · graph-structure 1.0 · graph-referral-source 1.0
graph-connection 0.6 · not-covered 0.0 (abstention 0.0)
hits by rank: #1 ×28 · #2 ×8 · #3 ×9 · #4 ×3 · #5 ×4
```

**`make kb-eval` exits 1 on purpose.** `forbidden_violations` is non-empty —
all six `gap-*` questions returned a chunk from their `must_not_return` — and
the harness treats confabulation as failing. Correct behaviour, and the exact
defect health.studio's FINDINGS predicted on 2026-08-16 ("an evidence floor
would delete the abstention machinery"): raw vector retrieval cannot abstain.
The five not-covered questions expect `graph/body-graph-index.md` — the
closed-world chunk whose whole content is "a connection absent from this list
is one the system does not know" — and it came back for **none of them**.
Nearest-neighbour muscles came back instead: asked about a nerve the corpus
doesn't model (gap-01), it returns five nerves it does; asked about hip ROM
(gap-06), it returns the shoulder joint. Abstention 0.0.

**gap-04 (cardiac) is the nuance worth reading precisely.** It HIT — the
machine-readable red-flag screen is at rank 3 — but rank 1 is
`referral-source--diaphragm_referral`, which is in its `must_not_return`. The
predicted scalenes/upper-body confounders never appeared; the actual violator
was a different referral-source chunk. So the ER answer is *present but
outranked* by a reassuring look-alike. That is a ranking problem, not a
coverage problem, and it is precisely what a reranker or the safety-pin
filter is for.

**The nine real misses cluster, and the cluster is the payload argument.**
6 of 15 graph-connection questions missed, plus chain-03, nerve-04, entr-02.
Two shapes:

- *Lost to prose that legitimately covers the topic* (conn-07 arch→foot-core,
  conn-08 breathing→the mechanism chunk, chain-03 balance→vision-and-balance).
  A human might call these right answers; the key says the graph chunk should
  win for a "which muscle" question. Ranking, not absence.
- *Lost to an oversized referral-source chunk stuffed with symptom vocabulary*:
  conn-09 ("deep buttock pain shooting down the leg" → **uterus_prostate** at
  rank 1, piriformis nowhere in top 5), nerve-04 (forehead/eye pain →
  **paranasal_sinus** over trigeminocervical). These are the largest chunks in
  the corpus (2,435–2,626 words, ~half bibliography) winning queries they
  shouldn't. This is the measured version of the dilution the bibliography
  finding predicted — now it has victim question ids, not just word counts.

**What held.** `stamp_integrity` 1.0 over all 330 returned passages: every
passage arrived carrying its complete evidence legend — `NONE` chunking
verified at the retrieval end, not just at ingestion. Prose recall is perfect;
the graph's structure and referral-source strata are perfect on their *own*
questions (the referral sources retrieve fine when they're the answer — the
problem is they also retrieve when they're not).

Next moves this baseline prices, none started: the bibliography move (now with
conn-09/nerve-04 as its before/after probes, and only buildable on the Aurora
KB), a reranker on the retrieve path (no re-ingest), and an abstention
mechanism (score threshold or making `body-graph-index` findable) for the
not-covered class. Each is now a signed delta against 0.7879, which was the
entire point of running the baseline first.

## 2026-08-16 — miss-rank probe: 7 of the 9 baseline misses sit at rank 7–19

The baseline says *what* missed; it can't say *how far* a miss was. One
read-only probe answers that: re-run the 9 missed questions (misses = expected
chunk absent from top-5, not-covered class excluded) through the identical
shipped path at `top_k=25` and record the expected chunk's true rank.

```
uv run --with boto3 python .scratch/kb-miss-rank-probe.py   # 9 retrieve calls
```

| question | expected chunk | rank @25 |
|---|---|---|
| conn-06 | connection--sternocleidomastoid | 7 |
| conn-08 | connection--diaphragm | 8 |
| conn-09 | connection--piriformis | **11** |
| conn-10 | connection--quadratus_lumborum | 11 |
| entr-02 | entrapment--thoracic_outlet | 12 |
| conn-07 | connection--tibialis_posterior | 13 |
| nerve-04 | nerve--trigeminocervical | 19 |
| conn-04 | connection--scalenes | absent |
| chain-03 | chain--LL | absent |

**Consequence: a reranker over a widened pool (retrieve 25 → rerank → cut to
5) has 7 rescuable misses to work with, conn-09 and nerve-04 included.** Ceiling
if it rescues all 7: recall@5 0.7879 → ~0.96 on the answerable set. No corpus
change, no re-ingest — this is the cheap lever, and it now has a measured upper
bound instead of a hope.

The two absent-at-25 misses fail differently and a reranker cannot touch them:

- **conn-04** (scalenes): loses to its own neighbors — top-3 are temporalis,
  deep_neck_flexors, levator_scapulae. The embedding puts a dozen neck-region
  connection chunks in a tight cluster and scalenes isn't the nearest; only
  changing what gets embedded (bibliography strip / chunk rewrite) moves it.
- **chain-03** ("a line that handles side-to-side balance" → chain--LL): a
  vocabulary gap, not dilution — top-3 are balance/vision prose and the
  meridians overview. The Lateral Line chunk evidently never says "side-to-side
  balance" in words the embedding can latch onto. Fix is content-side phrasing
  (or the reranker's query-document attention, but at rank >25 it never enters
  the pool).

So the roadmap's two levers now have a clean division of labour: reranker for
the 7 near-misses, corpus work (bibliography move on Aurora, phrasing fixes)
for the 2 deep ones. Probe artifacts: `.scratch/kb-miss-rank-probe.{py,json}`.

## 2026-08-17 — the live stack is now asserted, and it was already 41 keys stale

Two things came out of `05-chunk-shape`'s first live round-trip attempt, and the
second one was not what anybody was looking for.

**1 · `get_data_source` closes the four-day gap, and it is not vacuous.**
`test_kb_chunking_contract.py` reads Pulumi source with an AST; it was green for
the entire four days the deployed data source said `FIXED_SIZE`. The new
`packages/core/tests/test_kb_data_source_live.py` reads the *account*. Against
`JDNNGSU1JT` / `U0PM4HIXGE` (us-east-1, dev) the whole deployed block is:

```
"vectorIngestionConfiguration": {"chunkingConfiguration": {"chunkingStrategy": "NONE"}}
```

— one key, no `fixedSizeChunkingConfiguration`. Proved non-vacuous by inverting
both assertions: they fail printing the real value (`assert 'NONE' == 'FIXED_SIZE'`,
`assert 'fixedSizeChunkingConfiguration' in {'chunkingStrategy': 'NONE'}`), so
the pass is a reading of the cloud and not an empty parametrize.

It is opt-in (`DOCPIPE_LIVE_STACK=1`, `make kb-live-check`) because
`conftest.py` poisons AWS credentials for the whole unit suite on purpose. An
opt-in test nobody opts into is a proof nobody runs, so the skip names its own
invocation (`pytest -rs` prints it, 4 of 4) and `test_the_live_check_is_reachable`
runs in the default `make test` and fails if the target or its variables vanish.
Suite: 93 → 94 passed + 4 named skips; 5/5 with the switch on.

**2 · The bucket was 41 sidecars stale, and nothing in either repo could tell.**
Diffing `build/kb` against `s3://docpipe-dev-kb-source-733866507398/corpus/`
before any write: 766 remote keys, 992 local, **41 keys changed** — every one a
prose `*.md.metadata.json`, every one a `citationCount` that reads `0` remotely
and `1` locally. That is health.studio `#18` (`a634a36`, 06-structure-provenance)
landing on main and never being synced. The `.md` bodies are byte-identical; only
the sidecars drifted.

The consequence is a measurement one, and it matters for anything that quotes
03's baseline: **recall@5 0.7879 was measured against an index whose metadata is
not health.studio main's.** `citationCount` is `includeForEmbedding: false`, so
the vectors are unaffected and the retrieval numbers stand — but any filter or
rerank that reads it was reading a stale value, and the next sync moves 41 keys
for a reason that has nothing to do with the chunk split it will be blamed on.

The detector already existed and nobody was running it: `plan_prune` /
`sync.py --dry-run` prints exactly this diff and mutates nothing. Cheap standing
habit — dry-run the sync after any health.studio KB merge, not only before an
ingest.
## 2026-08-17 — the reranker, measured: +0.1139 MRR, four rescues, and two questions it broke

`retrieve(rerank=True, rerank_pool=25)` was scored against the live KB on the
same 66-question held-out set, the same index (`JDNNGSU1JT`, job `QPOVWZN9JY`,
496 vectors) and a byte-identical corpus (`sha256:102455d7…`). Only the retrieval
config moved. Row: how2doo `goals/kb-retrieval-readiness/inventory/corpus-census.json#scores[3]`.

| | raw | rerank pool=25 | Δ |
|---|---|---|---|
| recall@5 | 0.7879 | **0.8182** | +0.0303 |
| MRR | 0.5634 | **0.6773** | **+0.1139** |
| hits at rank 1 | 29 | **39** | +10 |
| stamp integrity | 1.0 (330 passages / 200 chunks) | 1.0 (330 / **207**) | 0 |

**The headline is not the verdict, and the pre-registration is why that is
legible.** health.studio's set pre-registered the acceptance test *before* the
run — conn-09 **and** nerve-04 flip miss→hit — and a paired rule: real only at an
exact two-sided binomial p < 0.05 on the discordant pairs. Measured: conn-09
flipped to **rank 1**, nerve-04 did not flip at all, and the paired test gives
**d = 6 but 4 versus 2 → p = 0.6875**. The hit/miss change is inside noise by the
rule that was agreed in advance, even though both headline metrics rose.

**Two questions were lost, and both are diagnostic rather than random.**

- **biblio-01** — *"What are the sources behind your claims about myofascial
  chains?"* was rank 2, now absent. The cross-encoder returned the five chain
  chunks (SBL, LL, DFL, SFL, BFL) and dropped
  `anatomy/01-myofascial-chains--09-sources.md`: **it matched the topic and
  discarded the word "sources".** biblio-02 (identical shape, pain science) and
  biblio-03 (bibliography inside a content chunk) both held at rank 1, so this is
  a specific failure, not a whole-class one — a cross-encoder does not
  systematically hate bibliographies, it out-ranks *this* one against five chunks
  named for the thing the question names.
- **nerve-01** — *"ring and little fingers go numb — which nerve?"* returned five
  ulnar-**territory** items (`lumbrical_ulnar`, `carpal_tunnel`,
  `abductor_digiti_minimi_hand`, `brachial_plexus-lower_trunk`,
  `extensor_digiti_minimi`) and not the ulnar **nerve** chunk. That is the
  muscle-vs-nerve discrimination the question set exists to test — the same
  discrimination reranking *fixed* at conn-09. **Same mechanism, opposite sign,
  same run.**

**This repo's own miss-rank probe (`b0d7d1e`) is partly refuted, and its blind
spot is now measured.** It predicted 7 rescuable misses at pool 25; **four
landed** (conn-06 @4, conn-07 @1, conn-09 @1, entr-02 @3), **conn-08, conn-10 and
nerve-04 did not**, and nothing outside the predicted set was rescued. It could
not predict either loss, because it only re-ran the *misses*: **a rescue-only
probe is structurally blind to what a reranker gives back.** Any future "what
would X rescue" probe should re-rank the hits too, or say out loud that its
ceiling is one-sided. (Fair caveat: the probe measured the 383-chunk corpus, this
run the 496-chunk split, so pool composition moved between them.)

One improvement worth naming because it is safety-adjacent: **gap-04** (*"can a
heart problem cause pain down the left arm?"*) now returns the emergency
red-flag screening chunk at **rank 1** (was 4) — though `gallbladder` and
`diaphragm_referral` are still in its top 5, so it remains a forbidden violation.

Nothing was adopted: `retrieve()` still defaults `rerank=False`, and
`test_rerank_off_sends_the_pre_rerank_request_shape` still pins the raw path
byte-identical. Whether to turn it on is health.studio's call, made against these
numbers.

## 2026-08-17 — `make kb-eval RERANK=--rerank` cannot run: 3 requests per minute, not adjustable

The reranked eval fails with `ThrottlingException` on `Retrieve` — under
botocore's default retries, and again under `AWS_RETRY_MODE=adaptive` with
`AWS_MAX_ATTEMPTS=12`, which spent about eleven minutes before giving up. The
error text is about request rate and the instinct is to retry harder. That is
wrong, and one read-only call says so:

```
aws service-quotas get-service-quota --service-code bedrock --quota-code L-11512E58
→ "On-demand model inference requests per minute for Cohere Rerank 3.5" = 3
  QuotaAppliedAtLevel: ACCOUNT   Adjustable: false   (no change request on file)
```

**66 questions × one rerank call each therefore needs ≥22 minutes of wall clock
however the client behaves.** botocore's backoff caps at 20 s per attempt because
it is built for bursts, not for a 20-second-per-request budget — so every attempt
it makes inside that window is another throttled request.

The fix is pacing, not retrying, and it does not belong in the harness: a scratch
wrapper (`.scratch/kb-eval-paced.py`) drives the **committed** `score_question_set`
through a botocore client whose `retrieve` is spaced 22 s apart, and changes
nothing else — same request body, same scorer, same metrics, same report shape.
Result: **66 calls, 0 throttles, 1,432 s**. The `Rerank` requests-per-second quota
(`L-0DD4EC92`, 10/s) is a decoy; the model's per-minute quota binds first.

Budget a reranked eval as ~25 minutes and ~$0.13, not as "one eval like the
baseline". A pass that budgets it the other way times out and reports that model
access is broken, when it is not.

## 2026-08-18 — V3.2 drives a multi-search tool loop unprompted: 3 retrievals, self-set evidence floor, end_turn at call 4

The 2026-08 smoke test proved one `tool_use` round; this ran the whole loop.
A hand-written Converse loop (`.scratch/converse-tool-loop.py` — no Strands,
every message visible) with a single `search_kb(query, min_evidence)` tool,
`deepseek.v3.2`, live KB `JDNNGSU1JT`, question "What connects the shoulder to
the neck, and how strong is the evidence?":

- **The model reformulated its own queries.** Three `tool_use` rounds with
  three distinct phrasings ("shoulder neck anatomical connections muscles
  ligaments" → "…trapezius muscles anatomy" → "cervical spine shoulder
  connection anatomy"), each pulling a *different* body-graph edge chunk.
  Multi-retrieval — the concrete thing an agent buys over the fixed
  `ChatClient.reply` pipeline — happened with zero prompting.
- **It set `min_evidence=1` on every call by itself**, from a one-line hint in
  the tool description. The retrieval floor works as a per-call model decision,
  which is the design argument for exposing it as a tool parameter.
- **The stateless tax, measured:** input tokens per call 460 → 1,221 → 1,976 →
  2,750; one question = 4 converse calls, 6.4k input / 0.65k output tokens
  (~$0.005 at V3.2's $0.62/$1.85 per M). Budget agent questions at ~4× the
  single-shot cost.
- **`end_turn` arrived at call 4 of a `MAX_ITERATIONS=5` bound.** The bound is
  not decorative; a tool-happier model or a wider tool surface hits it.
- Rerank stayed OFF in the tool: three tool calls in one question would eat the
  3 req/min account-level Cohere quota (see 2026-08-17 entry) on its own.

## 2026-08-19 — corpus is now 496 chunks; 72 are bibliography-only and filterable

Cross-ref: measured in health.studio (owner of the exporter), recorded at
`specs/040-chunk-shape/findings.md` there. What matters on the docpipe side:
the heading split ran (kb-data-status item 2 is done — 41 parents → 154 pieces,
79 still over the 1024-token ceiling), and it minted the `### Sources` sections
as 72 standalone chunks. Every sidecar tags them `section: "Sources"`, so
retrieval can exclude them with a metadata filter today — no rebuild needed —
or ingestion can skip them. The 383-chunk figures in `docs/kb-data-status.md`
are stale. Nerves backlog for future sessions:
health.studio `specs/041-nerve-registry/bugs.md`.

## 2026-08-19 — the case for Aurora consolidated: three product features, not one benchmark

What accumulated today, recorded so the re-ruling moment has it in one place.
The 2026-08-16 ruling (ship on S3 Vectors; Aurora + benchmark deferred) stands
until the human moves it — nothing below is applied. **Moved the same day:**
the human re-ruled Aurora must-have (PLAN.md 2026-08-19 revision), and the
three applies ran 2026-08-20 — see that entry below. This paragraph is the
before-picture, kept as written.

Aurora-exclusive, cannot be had on S3 Vectors at any price:

1. **Hybrid search.** The GIN `to_tsvector` index is already in
   `scripts/aurora_bootstrap.py`; the only code gap is `overrideSearchType`
   missing from `retrieval.py` (~1 param). The corpus is unusually entity-heavy
   (Latin muscle names, nerve ids) — the lexical channel targets exactly the
   dense-retrieval misses. Check the baseline's miss-rank probe for the lexical
   signature before crediting this.
2. **The bibliography move** (kb-data-status Decision 2): ~12 KB bibliographies
   fit Aurora's GIN-indexed jsonb, and text moved to metadata leaves BOTH
   channels — semantic and lexical. Parked with Aurora by the ruling.
3. **chatCompacts as searchable memory**: a separate app table (never
   `bedrock_integration.bedrock_kb`, never the shared KB — per-user data stays
   out of the public corpus), pgvector + tsvector + userId column, queried over
   the Data API directly. DynamoDB stays source of truth for chat state; the
   ~15s resume must never sit in the first-message path.
4. **Rerank-quota relief.** Cohere is 3 req/min account-wide and OFF in the
   agentic path. If hybrid closes part of the 0.7879 → ~0.96 gap for free, the
   dependency shrinks where it hurts most.

Cost re-estimate (prices verified 2026-08-13, PLAN.md): floor ~$0.12–0.50/mo
paused; realistic $5–15/mo with pre-warm keeping the cluster awake through
active hours; $44/mo only if something keeps it awake 24/7 — a bug, not a plan.

The counterweight, so this entry is not a sales pitch: **shipping does not need
Aurora.** Rerank over a widened pool already measures ~0.96 on the answerable
set, S3-only. Aurora is must-have on the roadmap (it is the locked "intended
primary"); whether it jumps ahead of shipping is a time allocation, and the
ruling that decides it should cite this entry either way.

## 2026-08-20 — the three Aurora applies ran; four first-run bugs, all four now fixed in code

PLAN.md's warning ("the Aurora branch in `data.py` has never executed — treat
the first apply as a test") paid out four times — one bug per run, each masking
the next:

1. **EC2 rejects non-ASCII in a security-group description.** The em-dash in
   `"Aurora — Postgres reachable only from inside the VPC"` fails
   `CreateSecurityGroup` with `InvalidParameterValue: … Character sets beyond
   ASCII are not supported`. Tags take Unicode; `GroupDescription` does not.
   This repo's prose style (em-dashes everywhere) makes it a live hazard in any
   user-visible AWS string — fixed to an ASCII hyphen in `data.py`.
2. **RDS retires patch versions out from under a pin.** `engine_version:
   "16.6"` — valid when written 2026-08-14 — now fails `CreateDBCluster` with
   `InvalidParameterCombination: Cannot find version 16.6 for
   aurora-postgresql`. `describe-db-engine-versions` lists 16.8–16.14 for
   us-east-1 today; repinned to 16.14. A pinned minor is a time bomb with a
   ~1-year fuse: check the live list, not the docs, when this recurs.
3. **The RDS Data API rejects multi-statement SQL.** `aurora_bootstrap.py`'s
   final "grants" entry packed two `GRANT`s into one `ExecuteStatement` →
   `ValidationException: Multistatements aren't supported`. Everything before
   it (role, secret, extension, table, all three indexes) had already applied —
   idempotency is what made the re-run after the split safe. One statement per
   Data API call, always.
4. **Bedrock validates the Aurora KB by calling `rds:DescribeDBClusters` as
   the KB role.** `CreateKnowledgeBase` failed 403 after ~135 s with exactly
   that action named. It is not in any of the AWS-documented rds-data actions
   the role obviously needs — added as its own statement in `kb_aurora.py`.
   The 135 s before the failure is the cluster resuming from auto-pause:
   Bedrock's validation call itself pays the min-ACU=0 resume.

Also learned operationally: an interrupted `pulumi up` (Ctrl-C mid-update)
leaves the S3-backend lock held by a dead pid; the next `up` refuses with
"the stack is currently locked". `pulumi cancel` clears it — verify the pid is
dead first (`ps -p <pid>`), because cancel on a *live* update corrupts state.

One more state-surgery lesson: a `pulumi up | head`-style pipe kills Pulumi
mid-update when the pipe closes (SIGPIPE reads as KeyboardInterrupt in the
language host). One such kill landed between CreateDBInstance and the state
write, leaving the instance live in AWS but absent from state → the retry
failed `DBInstanceAlreadyExists`. Repair that worked: confirm in AWS the
resource is healthy, `pulumi refresh --clear-pending-creates --yes`, then
`pulumi import aws:rds/clusterInstance:ClusterInstance docpipe-dev-aurora-1
docpipe-dev-aurora-1 --parent docpipe-dev=<Data component URN> --protect=false`
— after which `pulumi preview` showed 58 unchanged. Never pipe an apply
through `head`; redirect to a file and read the file.

End state, verified live 2026-08-20: cluster `docpipe-dev-aurora` ACTIVE
(16.14, Serverless v2 min 0 / max 1 ACU, auto-pause 300 s, Data API on),
`bedrock_integration.bedrock_kb` + 3 indexes + `bedrock_user` in place, and
the second KB is real: **`A44CISMRAM`** (ACTIVE, storage RDS), data source
**`H8PN3JBXPN`**, exported as `aurora_knowledge_base_id` /
`aurora_kb_data_source_id`. The benchmark's second store exists; next is the
sync fan-out + ingest, then the Phase 5b measurements.

Budget first applies of never-run branches as N short failing runs, not one
long one — the same one-error-per-apply cadence as the 2026-08-13 first
deploy.

## 2026-08-20 — first Aurora ingest: 496/496, and the two KBs no longer hold the same corpus

Job `B6RTYKM3K9` on KB `A44CISMRAM`: scanned 496, indexed 490, **failed 6**
with only `"The server encountered an internal error"` — no document names, no
error class. A plain re-run (job 2) indexed exactly those 6 as *modified*, 0
failed. So: opaque internal errors on an Aurora ingest are transient; re-run
before investigating. `SELECT count(*)` over the Data API confirms **496 rows**
in `bedrock_integration.bedrock_kb` — per the standing rule, the store was
verified directly, not trusted from job stats.

~~The number that matters for Phase 5b: the source bucket holds the **496-chunk
corpus** (the health.studio re-sync already landed), so Aurora indexed 496 —
but the S3 Vectors KB (`JDNNGSU1JT`) still holds **383 vectors** from the
pre-06 corpus and its 0.7879 baseline was measured on those. The two stores
are NOT currently comparable.~~ **RETRACTED 2026-08-20, same day:** the 383
figure was an inference from the 2026-08-16 runbook, not a measurement. A
fresh ingestion job on `JDNNGSU1JT` returned 0 new / 0 modified / 0 failed,
and paginated `list_vectors` counts **496** — the S3 Vectors KB had already
been re-ingested on the 496 corpus before today. The stores were comparable
all along; the benchmark ran the same day (next entry).

## 2026-08-20 — first cross-store benchmark: same recall, Aurora semantic ranks far worse, hybrid buys the rank back

Four runs, one day, both KBs holding the byte-identical 496-chunk corpus,
answer key v2 (66 questions, ratified 2026-08-17, every expected and forbidden
key verified present in the bucket before spending a call). k=5, no rerank,
`min_evidence: null`. Reports committed under `docs/baselines/2026-08-20-*`.

| run | recall@5 | MRR | note |
|---|---|---|---|
| S3 Vectors, raw | **0.7879** | **0.5634** | vs 0.7879 / 0.5538 on the 383 corpus — the corpus change was recall-neutral and rank-neutral on this key |
| Aurora, raw (semantic) | **0.7879** | **0.3172** | identical recall, **MRR nearly halved**, degradation uniform across strata (prose 0.8058 → 0.3993) |
| Aurora, HYBRID | 0.7727 | 0.5331 | recovers most of the rank deficit for −0.015 recall (one graph-chain miss) |
| S3 Vectors, HYBRID | — | — | **rejected by the API**: "HYBRID search type is not supported for search operation on index JDNNGSU1JT" |

What this says:

1. **Recall is a corpus property here, not a store property** — 0.7879 on both
   stores, and unchanged from the 383-chunk baseline. The 0.7879 → ~0.96 gap
   still belongs to the reranker/hybrid layer, not to either index.
2. **Aurora's semantic-only ranking is the anomaly worth chasing.** Same
   embeddings, same cosine metric, same top-5 membership, systematically worse
   ordering. Suspects: Bedrock's score normalisation on the RDS path, or HNSW
   `ef_search` at its default in Bedrock's session — note Bedrock issues the
   SQL, so `hnsw.ef_search` being a session knob means *we cannot set it* for
   Bedrock-issued queries; the PLAN 5b sweep as written only applies to a
   direct-SQL harness, not to the shipped Retrieve path.
3. **Hybrid is real and Aurora-exclusive, by measurement on both ends** — the
   GIN `to_tsvector` index earns its keep (MRR +0.216 over Aurora semantic),
   and S3 Vectors refuses the mode outright, settling the "still unverified"
   note in `retrieval.py`'s docstring.
4. ~~Ranking champion today: S3 Vectors raw (MRR 0.5634), with Aurora HYBRID
   0.03 behind.~~ **Superseded same day** — the Aurora semantic number was an
   inverted-ordering artifact; corrected, the stores tie (see the score-
   inversion entry below), and hybrid's "recovery" was rescuing the broken
   order, not beating healthy semantic. The decision layer (rerank over
   widened pool, ~0.96 measured ceiling) sits above both and was not in
   these runs.

All four runs exit 1 by design: the not-covered stratum still returns
confabulation magnets (abstention 0.0, n=5) — unchanged from the known gap-04
class, and hoisted `forbidden_violations` is doing its job.

## 2026-08-20 — "wtf with aurora" solved: Bedrock's RDS path returns cosine DISTANCE as score and sorts worst-first

The MRR anomaly from the benchmark entry above is a Bedrock service bug on the
RDS storage path, pinned to six decimal places:

1. **Symptom, from the committed reports alone (no AWS calls):** 65/66
   questions return the identical top-5 set from both stores; **63/66 in
   exactly reversed order.** Ranks mirror (1↔5, 2↔4).
2. **Ingestion exonerated:** the stored vectors for the same chunk, pulled
   from Postgres (Data API) and from S3 Vectors (`get_vectors`), are
   bit-identical — cosine 1.000000, unit norm, 1024 dims.
3. **Ground truth:** embedding the query with Titan v2 directly:
   `cos(query, pain-science--03) = 0.227910`, `cos(query, red-flags--04) =
   0.167616`. S3 Vectors reports `(1+cos)/2` (0.613985, 0.583809 — exact) and
   orders correctly. **Aurora reports `1 − cos` — the cosine distance itself
   (0.772090, 0.832384 — exact) — and sorts descending: most-distant first.**
4. The candidate *set* is the true top-k (that is why recall tied at 0.7879);
   only the response ordering is inverted. HYBRID (RRF fusion scores,
   `0.500000` at rank 1) and reranked lists order correctly. Worth an AWS
   support case; nothing in our config causes it.

**Fix:** `KnowledgeBaseClient(semantic_score_is_distance=True)` re-sorts the
raw semantic path ascending, touches nothing else (4 unit tests pin the
scope). `--semantic-score-is-distance` on the eval CLI; the report records it.

**Corrected numbers** (`docs/baselines/2026-08-20-aurora-496-scorefix.json`):
Aurora semantic **0.7879 / MRR 0.5646** vs S3 Vectors 0.7879 / 0.5634 — the
stores are equivalent on semantic ranking, exactly as identical embeddings
demand. Two honest revisions to the entry above: the "ranking champion" is a
tie, and **HYBRID (0.7727 / 0.5331) is a slight net NEGATIVE against healthy
semantic on this golden set** — its apparent rescue was measured against the
inverted ordering. The lexical-channel hypothesis did not show a net win at
k=5; hybrid remains the tool for entity-heavy misses, to be judged per-miss,
not a default.

**Latency, warm (2 passes over the 66 questions, k=5, one client):**

| | p50 | p95 | mean | n |
|---|---|---|---|---|
| Aurora (awake) | **0.523 s** | **0.873 s** | 0.568 s | 131 |
| S3 Vectors | 0.960 s | 1.797 s | 1.033 s | 130 |

Aurora is ~2× faster warm at both percentiles. S3 Vectors also threw one
`ThrottlingException` at ~1 rps — its Retrieve rate ceiling is low enough to
matter for a fleet. Cold start (>5 min idle, min-ACU 0 resume) measured
separately below. Instrument: `.scratch/kb_latency.py` (gitignored; promote
to `scripts/` if it should run in a gate).

## 2026-08-20 — Aurora cold start on the Retrieve path: 1.32 s, not ~15 s

Measured properly, with the pause verified rather than assumed: CloudWatch
`ServerlessDatabaseCapacity` shows the cluster at **0.0 ACU from 18:42 to
18:56** (paused for >14 min), a single timed `Retrieve` fired at ~18:57, the
same minute shows the wake to 0.5 ACU — and the call returned in **1.32 s**.
n=1, but the trace removes the usual doubt about whether the sample was
actually cold; a rerun costs one quiet 7-minute window.

The standing "~15 s resume" figure (bootstrap docstring, the 2026-08-19
chatCompacts caveat, the cost entry) was never a measurement of this path: it
came from `aurora_bootstrap.py`'s Data API experience, where the resume
surfaces as `DatabaseResumingException` and **our own retry sleeps (10 s,
20 s) are most of the elapsed time**. `StartIngestionJob` also throws
"resuming... try again" rather than waiting. The Bedrock Retrieve path
absorbs the resume instead of throwing, and does it in ~1.3 s.

Product consequence, plainly: the first-message-after-idle latency concern
that shaped the chatCompacts design caution is ~1.3 s, not ~15 s — auto-pause
at min-ACU 0 is compatible with the user-facing retrieval path as measured
today. Re-verify before load-bearing use (n=1, and AWS resume behavior is
theirs to change), but every current number says the cost floor and the UX
can coexist.

**Amended same day, two more samples:** Retrieve against a paused cluster
does not always absorb the resume — both Aurora agentic runs died on their
first call with `ValidationException: … The Aurora DB instance … is resuming
after being auto-paused. Please wait a few seconds and try again.` So the
cold path is bimodal: fast success (1.32 s, once) or an instantly-retryable
error (twice). Either way the ~15 s stall was never observed; the caller
contract is "retry that one ValidationException message", now implemented in
`kb_agentic_eval.py::_retryable` — and any production caller on an Aurora KB
needs the same handling (`retrieval.py` does not retry; its callers must).

## 2026-08-20 — agentic rows land: the loop beats every single-shot row, and Aurora agentic hits 0.9508

`scripts/kb_agentic_eval.py`, V3.2 driving `search_kb` (its own reformulations
and `min_evidence` choices, ≤5 iterations, k=5 per call, rerank OFF), scored
as **session_recall over the 61 answerable questions** — not MRR, a
multi-query session has no single ranked list. Reports:
`docs/baselines/2026-08-20-*-agentic.json`.

| strategy | recall | mean tool calls | p50 wall | tokens/run |
|---|---|---|---|---|
| S3 Vectors single-shot | 0.7879 | 1 | 0.96 s | — |
| Aurora single-shot (fixed) | 0.7879 | 1 | 0.52 s | — |
| S3 Vectors agentic | **0.8852** | 2.92 | 15.4 s | 485k in / 35k out |
| Aurora agentic (fixed) | **0.9508** | 2.88 | 13.6 s | 483k in / 36k out |

What holds and what does not:

1. **The agent's multi-query loop is worth ~+0.10–0.16 recall over single-shot
   retrieval** — reformulation recovers questions where the first phrasing
   misses. 42–43 of the hits land on tool call 1; the rest are earned by
   calls 2+, which is the loop paying for itself.
2. **Do NOT read Aurora-agentic 0.9508 vs S3-agentic 0.8852 as a store
   effect.** The delta is 4 questions on a single run each at temperature
   0.3 with model-chosen queries — run-to-run variance is unmeasured, and the
   single-shot rows say the stores tie on ranking. Claiming a store win here
   needs repeated runs; until then the honest read is "both agentic rows
   ≥0.88, both beat single-shot".
3. **The stateless tax, priced:** ~483k input tokens per 66-question run ≈
   **$0.36/run ≈ $0.0055/question** at V3.2 rates, and p50 ~14 s vs ~1 s
   single-shot. The agentic win is not free; it is 14× slower and ~4 converse
   calls per answer (matches the 2026-08-18 single-question measurement).
4. **Agentic retrieval does not fix the not-covered gap:** abstention 0.0 and
   6 forbidden-magnet violations in both runs — wider recall pulls the
   confabulation magnets in more often, not less. Abstention remains a
   decision-layer problem (prompting/guardrail), not a retrieval-strategy one.

The Aurora HYBRID agentic run was stopped externally at question 37/66 —
no report written; rerun pending (~10 min).

## 2026-08-20 — six agentic runs: the 0.9508 was a high roll, hybrid is flat, and the union hits 61/61

Two runs of each agentic config (V3.2, temp 0.3), completing the matrix and
answering the variance question the first entry left open:

| config | run 1 | run 2 |
|---|---|---|
| S3 Vectors agentic | 0.8852 | 0.9016 |
| Aurora agentic (fixed) | 0.9508 | 0.9016 |
| Aurora HYBRID agentic | 0.9180 | 0.9180 |

1. **The store effect is zero, again.** Aurora-agentic run 2 landed exactly on
   S3-agentic run 2 (0.9016); the 0.9508 was single-run luck. Honest summary
   of all six: agentic retrieval sits at **0.89–0.95 on either store, any
   search mode** — the loop is the effect, the store is not.
2. **Hybrid in the loop is flat** (0.918 twice — eerily stable) — consistent
   with single-shot, where hybrid was mildly negative. Third strike for the
   lexical channel on this corpus; it stays a per-miss diagnostic tool.
3. **Reformulation loses what verbatim wins.** Only `prose-13` is missed by
   all six agentic runs — and single-shot HITS it (as it does `prose-06`,
   missed by 5 of 6). The agent rephrases away from a phrasing that already
   won. Agentic is not a superset of single-shot.
4. **The union is the result: single-shot ∪ agentic = 61/61 answerable
   questions.** The corpus can answer everything in the ratified key; no
   single strategy shows it. A production shape that runs the verbatim query
   as tool call 1 and lets the model reformulate from there would capture
   both — worth one measured run before Phase 3 settles the agent design.

Harness fix that made runs 5–6 possible: V3.2 sometimes emits **several
toolUse blocks in one message** (parallel searches), and Converse demands all
their toolResults in a single user message — one-message-per-result fails
with "Expected toolResult blocks at messages.N.content". The 2026-08-18
prototype has the same latent bug; fixed in `kb_agentic_eval.py` only.
