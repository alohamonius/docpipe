# Question bank

Answer cold, out loud, before reading the notes. `docpipe:` lines point at the
real answer in this repo. Grade yourself 0/1/2 per [`README.md`](README.md).

Legend: **Trap** = where confident candidates go wrong. **Follow-up** = where a
good interviewer escalates. **Senses** = the judgment question behind the
factual one, and the reason the question was asked at all.

---

## M0 — Rapid fire (phone screen)

Ten seconds each. These are filters; hesitation is the failure, not error.

1. **`Converse` vs `InvokeModel` — why does Converse exist?** Uniform
   request/response across providers (messages, `system`, `inferenceConfig`,
   `toolConfig`, `usage`), so the model id becomes configuration. `InvokeModel`
   is per-provider raw JSON. *docpipe: `llm.py:1-6, 89-96` — the whole reason
   swapping R1 → V3.2 was a config change.*
2. **Name the four Bedrock boto3 clients and what each is for.** `bedrock`
   (control plane: model access, guardrails, invocation logging, evaluations),
   `bedrock-runtime` (inference: Converse/Invoke), `bedrock-agent` (build-time
   control plane: KBs, data sources, `StartIngestionJob`), `bedrock-agent-runtime`
   (`Retrieve`, `RetrieveAndGenerate`, agent invoke). *docpipe: `kb_sync.py:8-13`
   documents this exact confusion; ingestion uses `bedrock-agent`, retrieval
   uses `bedrock-agent-runtime`.*
3. **What is an inference profile and why does `us.` prefix a model id?** It's
   the cross-region routing handle; `us.deepseek.r1-v1:0` routes within the US
   geography for up to ~2× in-region quota. Not all models have or need one.
4. **Two quotas throttle on-demand inference — name them.** RPM and TPM, per
   model per region. Either one → `ThrottlingException` / 429.
5. **`Retrieve` vs `RetrieveAndGenerate`.** The first returns passages, you own
   the prompt. The second is managed RAG in one call. docpipe uses the first
   because it injects passages into its own grounded system prompt.
6. **What does `chunkingStrategy: NONE` mean?** One file = one chunk = one
   vector. No splitting.
7. **Where do Knowledge Base metadata attributes come from?** A
   `<file>.metadata.json` sidecar next to each document in S3; Bedrock attaches
   them to every vector derived from that file and echoes them on retrieval.
8. **Cheapest way to halve a Bedrock bill for async work?** Batch inference
   (~50% discount). Then prompt caching, if the model supports it.
9. **What does a Bedrock guardrail do that a system prompt cannot?** It's
   enforced outside the model, so prompt injection cannot talk it out of the
   policy, and it's auditable and versioned independently.
10. **Embedding dimension is pinned in two places in docpipe. Which, and why?**
    The S3 Vectors index and the KB's `embedding_model_configuration`. Titan v2
    emits 256/512/1024 and an index cannot mix dimensions. *docpipe:
    `pulumi/components/kb.py:10-14, 77-86, 149-155`.*

---

## M1 — Bedrock core & inference economics

**M1.1 — You're on DeepSeek. Prompt caching supports Claude and Nova. What does that cost you?**
> Every turn re-sends the full grounded system prompt — base prompt +
> `_GROUNDING_INSTRUCTIONS` + k passages — as fresh input tokens. On Claude/Nova
> the static prefix would be a cache hit (~90% cheaper on that segment, ~85%
> lower latency). Strong answer quantifies: passages are the bulk of input,
> multi-turn chat re-pays for them on every turn.
> **Trap:** claiming caching would help anyway. It caches a *stable prefix* —
> and docpipe's retrieved passages change per query, so only the static system
> preamble would ever hit. The honest answer is "caching helps the preamble, not
> the passages; the real win would need a prompt restructure."
> **Senses:** would you switch models for a cost feature? Only with an eval set
> proving the swap doesn't degrade answers — which loops to M3.
> *docpipe: `llm.py:48-60`.*

**M1.2 — Requests start returning 429 in production. Walk me through your response, in order.**
> 1. Retry with exponential backoff **and jitter** (botocore's `adaptive` retry
> mode gets you client-side rate limiting for free). 2. Measure actual peak RPM
> and TPM from CloudWatch. 3. Quota increase sized from the measurement.
> 4. Cross-region inference profile (geo, then global) for ~2× headroom.
> 5. Provisioned Throughput **last** — thousands/mo, and only past roughly 30%
> sustained utilisation of the on-demand cap.
> **Trap:** jumping to Provisioned Throughput. Also: **PT does not work through
> inference profiles** — you choose one or the other, so "PT plus cross-region"
> is not a plan.
> **Follow-up:** what does your retry do to a queue-backed worker? (Retries
> extend visibility-timeout pressure; a poison message that always throttles
> must eventually hit the DLQ rather than retry forever.)
> *docpipe: `llm.py` — retries `2**attempt` **plus up to 1 s of jitter** (fixed
> 2026-08-17; was bare `2**attempt`, which synchronises a throttled fleet).
> botocore's `adaptive` retry mode remains the fuller answer — client-side rate
> limiting, not just desynchronisation — and is fair "what next" material.*

**M1.3 — Your retry catches three error codes. Which errors must you NOT retry, and why?**
> `ValidationException`, `AccessDeniedException`, `ResourceNotFoundException` —
> deterministic; retrying burns latency and hides the bug.
> `ModelStreamErrorException` mid-stream is its own case. Retrying a
> non-idempotent call that already charged you is money on fire.
> *docpipe: `llm.py:23-27`.*

**M1.4 — How do you swap the chat model without a code change, and what's the risk?**
> Converse makes the id configuration. The risk is silent behavioural drift:
> different models have different tool-use support (R1: none), different
> stop-reason semantics, different tokenizers (so your `maxTokens` budget
> shifts), and different scratchpad conventions.
> **Trap:** the `<think>` stripping regex. docpipe strips `<think>…</think>` —
> a model that emits an unterminated block, or uses different delimiters, leaks
> reasoning into user-visible output. That's a correctness bug, not cosmetic.
> *docpipe: `llm.py:29-30, 109-113`.*

**M1.5 — On-demand vs Provisioned Throughput vs Batch — pick one for each of: a chat assistant at 5 req/min, a nightly 200k-document classification, a 24/7 300 TPS pipeline.**
> On-demand; Batch (50% off, latency-tolerant); PT (sustained, predictable, past
> the utilisation crossover). Say the crossover out loud rather than guessing.

**M1.6 — What is Intelligent Prompt Routing and when is it the wrong tool?**
> Routes each prompt between two models **in the same family** to trade cost for
> quality. Wrong when you need cross-family routing, when you need deterministic
> model attribution for audit, or in batch (no documented support).

---

## M2 — RAG: corpus → chunks → vectors → retrieval

This is the core round, and docpipe's strongest material.

**M2.1 — Bedrock Managed Knowledge Base went GA on 2026-06-17 and collapses ingestion, parsing, chunking, embeddings, vector store and re-ranking into one primitive. Why did you build a custom KB instead?**
> The flagship question of this loop. Strong answer: **Managed KB's value is
> exactly the layers docpipe deliberately owns.** The corpus arrives
> *chunk-final* from health.studio's `pnpm kb:build` — 383 files, each already
> one retrievable unit carrying an evidence legend (★ scale + non-diagnosis
> disclaimer) in its header, plus a sidecar of evidence attributes that drive a
> retrieval-time floor. A managed pipeline that owns chunking and embedding is a
> pipeline that re-chunks a corpus whose chunk boundaries are the product.
> **Also honest:** for a corpus of ordinary PDFs with no chunk-level semantics,
> Managed KB is now the right default and docpipe's approach would be
> over-engineering. Say that. Refusing to concede where the managed service
> wins reads as defensiveness.
> **Follow-up:** "so when would you migrate?" — when the corpus stops being
> chunk-final, or when you need the six connectors and agentic multi-hop
> retrieval more than you need control of chunk boundaries.
> *docpipe: `pulumi/components/kb.py:167-188`, `FINDINGS.md` 2026-08-13.*

**M2.2 — Defend `chunkingStrategy: NONE`. Then tell me why `SEMANTIC` isn't the fix.**
> Each built chunk opens with a header carrying the ★ evidence scale and the
> disclaimer. *Any* splitter — FIXED_SIZE, SEMANTIC, HIERARCHICAL — strips that
> header from every fragment after the first, leaving the model bare star
> ratings with no scale attached. **SEMANTIC is a better splitter, and the
> problem is splitting at all.** That distinction is the whole answer.
> **Trap:** treating this as a chunk-quality question. It's a
> provenance-integrity question.
> *docpipe: `pulumi/components/kb.py:174-185`.*

**M2.3 — What breaks if a chunk exceeds the embedding model's token limit?**
> Titan v2 caps at 8,192 tokens. With `NONE`, one file = one vector, so an
> oversized file **fails ingestion rather than degrading** — a loud failure,
> which is the good kind. docpipe's largest chunk is measured at 24.7 KB /
> 3,694 words ≈ 5k tokens: fits, without much room.
> **Follow-up:** how would you catch this before it hits Bedrock? A pre-flight
> size guard in `CorpusSyncer.plan()` — added 2026-08-17: `OversizedDocRefused`
> refuses the whole plan, before any upload, when a document exceeds 32 KiB
> (8,192 tokens × a deliberately generous 4 B/token; the largest real chunk is
> ~4.9 B/token). Escape hatch `--max-doc-bytes`, same pattern as the
> blast-radius guard.
> *docpipe: `kb_sync.py` (`DEFAULT_MAX_DOC_BYTES`), `FINDINGS.md` sizing checks.*

**M2.4 — Changing `chunkingConfiguration` on a live data source: what actually happens?**
> It **replaces** the data source. `pulumi preview --diff` showed
> `+-1 to replace, 52 unchanged` with `dataSourceId: KPAQK6MQY4 => [unknown]`.
> Consequences: the id is not stable, anything holding `KB_DATA_SOURCE_ID` in
> env or `.env` points at a dead data source, and a **full re-ingestion** (and
> full re-embedding spend) follows.
> **Senses:** the operational lesson — resolve the id from stack output at
> runtime, never pin it in a `.env`. This is a genuinely non-obvious IaC gotcha
> and interviewers love that you measured it rather than read it.
> *docpipe: `FINDINGS.md`; the CLI's flags → env → `pulumi stack output`
> resolution order in `services/kb_sync/sync.py`.*

**M2.5 — Your `min_evidence` filter goes into the vector search config rather than filtering results in Python. Why does that matter?**
> Two reasons, and weak answers only give the first. (1) An excluded chunk never
> reaches the model and never consumes context or tokens. (2) **`top_k`
> semantics**: post-filtering returns *up to* k, often fewer, and silently
> degrades recall; pre-filtering returns k *qualifying* passages.
> **The filter is no longer a flat `greaterThanOrEquals`** (updated 2026-08-16):
> it is OR'd with `safetyCritical = true`. The flat floor was measured to be
> wrong in the worst direction — it deleted the machine-readable **red-flag
> screen** (`maxEvidence: 0` *and* `safetyCritical: true`, carrying the cardiac
> row) and made 11 of 66 golden questions unanswerable, because the corpus's
> honesty apparatus is unrated *by construction*. A floor meant to raise quality
> removed the ability to abstain and the safety net while keeping every
> confident claim — the exact inversion of its purpose. This is now one of the
> strongest stories in the repo; tell it.
> **Follow-up:** why does it default to `None`? Because the caller decides the
> floor — the health.studio code graph enforces ≥1 on its pain-reasoning path,
> but a general question shouldn't refuse unrated framing prose.
> *docpipe: `retrieval.py` (`retrieve()` docstring and filter), health.studio
> `docs/FINDINGS.md` 2026-08-16.*

**M2.6 — 97 of 383 chunks have `maxEvidence: 0` and 172 carry no citation. Is that a corpus bug?**
> No — framing prose and conceptual models are legitimate content. The bug would
> be letting them **retrieve identically to a systematic review**. The corpus is
> honest about its weak spots, so retrieval must not be. Two hooks separate
> them: `max_evidence` on the passage (so the reply can say which it is) and the
> retrieval-time floor.
> **Senses:** this is a question about whether you think "grounded" means "cited
> something" or "cited something *load-bearing*." Most candidates conflate them.
> *docpipe: `retrieval.py:9-18`.*

**M2.7 — A metadata sidecar is a `.json` file. Your corpus discovery globs `*.md`. What went wrong, and how did it fail?**
> `rglob("*.md")` **silently drops every sidecar**. You get a working KB whose
> every citation is an opaque S3 URI, with no retrieval-time evidence filtering
> — a silent quality failure, not an error. The fix collects a sidecar only when
> its `.md` is in the corpus (an orphan sidecar describes nothing).
> **Follow-up:** why count sidecars separately from documents? Because a sidecar
> produces no vector — reporting 766 "docs" for a 383-doc corpus is a lie that
> would make your ingestion stats meaningless.
> *docpipe: `kb_sync.py:116-131`, `PlannedDoc.sidecar` at `kb_sync.py:70-80`.*

**M2.8 — Your incremental upload compares S3's ETag to a local MD5. When is that wrong?**
> ETag equals content MD5 only for **single-part, SSE-S3 (AES256)** objects.
> Multipart uploads carry a `-N` suffix; SSE-KMS objects don't match either. The
> design is safe because a mismatch causes a **re-upload, never a false skip** —
> the failure direction matters more than the failure rate.
> **Trap:** "MD5 is insecure." It's change detection, not security; the code
> passes `usedforsecurity=False` precisely to say so.
> **Follow-up:** what if someone enables KMS on that bucket? Every sync
> re-uploads everything. Would you notice? (Only via the uploaded/skipped
> counts in the structured log.)
> *docpipe: `kb_sync.py:138-141, 191-200`.*

**M2.9 — Re-running the sync after editing one document: what do you pay for?**
> Locally: only changed bytes are re-PUT. On Bedrock: `StartIngestionJob`
> re-embeds only added/changed/deleted documents. So the cost is one document's
> embedding, not 383. Know both halves — candidates usually know one.
> *docpipe: `kb_sync.py:16-27`.*

**M2.10 — S3 Vectors vs OpenSearch Serverless vs Aurora pgvector. Choose, with numbers.**
> S3 Vectors: ~$0 at rest, serverless, GA Dec 2025 with up to 2B vectors/index —
> chosen here because it keeps the whole chat path permanently deployable.
> OpenSearch Serverless: ~$345/mo floor, but you get hybrid/BM25, filtering
> maturity, and low-latency ANN at scale. Aurora pgvector: you already have
> Postgres, joins against relational data, `min_capacity=0` auto-pause — at the
> cost of a cold-start on first query.
> **Senses:** the strongest version of this answer refuses to answer in the
> abstract and names the measurement that would decide it — which is exactly
> docpipe's Phase 5b: identical corpus, identical golden set, measure
> latency p50/p95, recall@k/MRR, and $ at rest + per 1k queries.
> *docpipe: README cost table; `PLAN.md` Phase 5b.*

**M2.11 — When is RAG the wrong answer?**
> When the answer isn't in a document (computation, live state, personalised
> data → tools/SQL, not retrieval); when the corpus is small enough to fit in
> context; when the query needs multi-hop reasoning over relationships (→
> GraphRAG / Neptune Analytics, GA Mar 2025); when freshness requirements are
> tighter than your ingestion cadence.
> **Senses:** knowing when *not* to reach for your favourite hammer is the
> whole question.

**M2.12 — S3 Vectors caps filterable metadata at 2 KB per vector. Are you under it?**
> Sidecars are ≤1,705 B as pretty-printed JSON with 9 attributes; the stored
> payload is smaller. **But this has not been confirmed against a real
> ingestion.** Say the last sentence. An unverified fit is a hypothesis, and
> presenting it as fact is how you fail a follow-up.

---

## M3 — Evaluation (the round most candidates fail)

**M3.1 — You changed the chunking strategy. How do you know retrieval got better?**
> A golden set of question → expected-passage(s) pairs, scored on recall@k /
> MRR / nDCG, run before and after. Without it you have an opinion.
> **docpipe has this now (since 2026-08-16), and it is the strongest material
> in the repo.** A 66-question set (authored 2026-08-14, ratified separately),
> baseline recall@5 **0.7879** / MRR 0.55, rank-1 hits 29/66. A miss-rank probe
> showed the rescuable misses sat at ranks 7–19, so the search pool widened to
> 25 with Cohere rerank 3.5 (cross-encoder) on top: recall@5 **0.8182**, MRR
> **0.68**, rank-1 **39**/66 — and a control re-run came back identical to four
> decimals with an identical miss set. Ships with two named reproducible losses
> (*biblio-01*, *nerve-01*) and a hard constraint: the rerank model is 3 RPM
> account-wide, non-adjustable. The reusable answer skeleton: *set → baseline →
> hypothesis → measured delta → named costs.*
> **The remaining honest gap is generation-side**: faithfulness, citation
> coverage, refusal rate on out-of-corpus questions — all still unmeasured.
> Retrieval is evaluated; generation is not. Say exactly that.
> *docpipe: `retrieval.py` (`retrieve()` docstring), `kb_eval.py`,
> `FINDINGS.md` 2026-08-16/17.*

**M3.2 — Separate retrieval quality from generation quality. Why does the distinction matter operationally?**
> They fail differently and are fixed differently. Bad retrieval → chunking,
> embeddings, k, filters, reranking. Bad generation with good retrieval →
> prompt, model, grounding enforcement. A single end-to-end score tells you
> something is wrong and nothing about where. Bedrock's RAG evaluation (GA
> ~Mar 2025) scores them separately for this reason.

**M3.3 — Name the metrics you'd put on a citations-required health assistant.**
> Retrieval: recall@k, MRR. Generation: faithfulness/groundedness, answer
> relevance, and specifically **citation coverage** and **citation precision**
> (added to Bedrock RAG evaluation) — for docpipe, "did it cite, and was the
> citation the passage it actually used?" Plus a safety-specific one: refusal
> rate on out-of-corpus questions, because the system prompt explicitly
> instructs "say so plainly instead of inventing facts" and that instruction is
> worthless unmeasured.
> *docpipe: `llm.py:48-52`.*

**M3.4 — LLM-as-a-judge: what are you actually trusting, and how do you keep it honest?**
> You're trusting a model to score a model — correlated failure modes, position
> and verbosity bias, and drift when the judge model is upgraded underneath you.
> Mitigations: pin the judge model and version, use a rubric with explicit
> criteria rather than a 1–10 vibe, calibrate against a human-labelled subset,
> and never use the same model as generator and judge for the claims that
> matter.
> **Follow-up:** when would you *not* use a judge? For hard-verifiable claims —
> where Automated Reasoning checks give you a sound formal answer instead of a
> probabilistic one.

**M3.5 — How do evals run in CI without burning money on every PR?**
> Tiered: a fast deterministic tier on every PR (retrieval-only, no generation —
> recall@k on the golden set is cheap and catches most regressions), a full
> generation eval nightly or on release tags, and a sampled online eval on
> production traffic. Gate merges on the cheap tier only.
> **Senses:** this is really a question about whether you've ever paid an LLM
> bill you didn't intend to.

**M3.6 — Your Phase 5b benchmark: what would make it dishonest?**
> Different corpora, different k, different embedding models, warm-vs-cold
> comparisons (Aurora auto-pause resume time counted for one store and not the
> other), a golden set written *after* seeing one store's results, and n too
> small to distinguish p95s. Naming the ways your own benchmark could lie is a
> strong senior signal.

---

## M4 — Agents & tool use

**M4.1 — Agents Classic is closed to new customers, AgentCore Harness went GA 2026-06-18. Justify Strands-in-Lambda.**
> Structure it as three live options, not one: (a) AgentCore Harness — managed
> Runtime/Memory/Gateway/Identity, two API calls, config not code; (b) AgentCore
> Runtime hosting your own framework code; (c) Strands in your own Lambda.
> docpipe picks (c) because the agent is a small tool surface (`search_kb` +
> history) behind an existing Lambda, the request path is latency-sensitive, and
> there's no standing cost. The decisive point: **one CLI command exports a
> harness as Strands code**, so neither direction is a one-way door.
> **Trap:** saying "Bedrock Agents" at all. Instant recency fail.
> *docpipe: `PLAN.md` locked decisions.*

**M4.2 — Walk the Converse tool-use loop, precisely.**
> Send `toolConfig` with the tool schemas → response comes back with
> `stopReason: "tool_use"` and a `toolUse` block (name, input, `toolUseId`) →
> you execute the tool → append an assistant message containing the `toolUse`
> and a user message containing a `toolResult` **keyed by the same
> `toolUseId`** → call again → repeat until `stopReason: "end_turn"`.
> **Trap:** dropping the assistant's `toolUse` block from history, or mismatching
> `toolUseId`. Both produce validation errors that look like model failures.
> **Follow-up:** what bounds the loop? A max-iteration cap and a token budget —
> otherwise a model that keeps calling tools bills you forever.
> *docpipe: verified in `FINDINGS.md` — DeepSeek V3.2 returned `stopReason=tool_use`
> with a sensible `search_kb` call at 400 total tokens.*

**M4.3 — When is an agent the wrong architecture?**
> When the control flow is known. A fixed retrieve → ground → answer pipeline is
> cheaper, faster, deterministic, testable, and cannot loop. Agents earn their
> cost when the number and order of steps genuinely depend on the input.
> **Senses:** docpipe's chat path is *already* a working non-agentic RAG
> pipeline (`ChatClient.reply` with passages). A good interviewer will ask what
> the agent buys you over it. Have a real answer — multi-step questions needing
> two different retrievals, or query reformulation — or concede that the
> pipeline is sufficient today.
> *docpipe: `llm.py:116-141`.*

**M4.4 — MCP: what is it, and what's the security story?**
> An open protocol for exposing tools/resources to models; AgentCore Gateway
> turns existing APIs into MCP tools; Strands supports it natively. The security
> story is the interesting half: **tool descriptions are untrusted input**. A
> malicious or compromised MCP server can inject instructions through a tool
> description or a tool result, which lands inside your model's context with
> apparent authority. Mitigations: pin/allow-list servers, treat tool output as
> data not instructions, scope credentials per-tool (AgentCore Identity), and
> keep a human approval step for state-changing tools.

**M4.5 — Strands vs LangGraph — pick one for a regulated health assistant.**
> LangGraph if you need auditable, explicit control flow — you own the graph and
> can prove which paths exist. Strands if you want a model-driven loop and
> native Bedrock/AgentCore integration with less scaffolding. For *regulated*,
> the explicit-graph argument is strong: "the agent decided" is a bad answer to
> a compliance question.
> **Trap:** picking on popularity. Pick on determinism.

**M4.6 — Where does conversation memory live, and what breaks at scale?**
> docpipe: DynamoDB, PK `userId` / SK `conversationId`, with TTL. Breaks: an
> unbounded history grows input tokens every turn (cost and latency both climb
> linearly), the 400 KB item limit, and hot partitions on a chatty user.
> Mitigations: windowed history, summarisation of older turns, or AgentCore
> Memory. Note the interaction with M1.1 — long histories are exactly what
> prompt caching would have paid for.
> *docpipe: `PLAN.md` Phase 3 `ConversationStore`.*

---

## M5 — Platform: IaC, IAM, networking, cost, observability

**M5.1 — pulumi-aws has no resource for S3 Vectors or model invocation logging. What did you do, and what did it cost you?**
> boto3-backed **dynamic providers**. The costs are real and specific:
> - Dynamic providers **do not inherit `aws:profile`** — that config only reaches
>   pulumi-aws. Raw boto3 falls back to the default credential chain (here:
>   expired creds → `AccessDeniedException`). Fix: pass the profile as a resource
>   prop and build `boto3.Session(profile_name=…)`.
> - Props **JSON-roundtrip through the engine: ints arrive as floats.**
>   `dimension: 1024` arrived as `1024.0` and boto3 rejected it. Fix: cast at the
>   call site.
> - A **failed `create` leaves real AWS orphans Pulumi doesn't know about** — the
>   vector bucket was created, then `create_index` raised, so the next `up` hit
>   `ConflictException`. Fix: make `create` idempotent, tolerate the conflict and
>   adopt the resource (names are deterministic and stack-owned; `delete()` owns
>   cleanup).
> **Senses:** the third one is the deep answer — it's really about **IaC not
> being transactional**, and every custom provider needing to be idempotent.
> *docpipe: `FINDINGS.md`; `pulumi/components/s3vectors.py`,
> `invocation_logging.py`.*

**M5.2 — Your KB trust policy has `Condition: StringEquals aws:SourceAccount`. What attack does that stop, and what would be stronger?**
> The **confused deputy**: without it, the `bedrock.amazonaws.com` service
> principal could be induced to assume your role on behalf of another account's
> resource. Stronger: `aws:SourceArn` scoped to the specific KB ARN, which
> docpipe already does on the Bedrock logs **bucket policy** but not on the
> **trust policies** — a real, nameable asymmetry in your own code.
> *docpipe: `pulumi/components/kb.py:99-102`, `safety.py:144-150, 174-177`.*

**M5.3 — Your KB role has `"Action": "s3vectors:*"`. Defend it.**
> Honestly: it's the least-privilege weak point. Defensible because it's scoped
> to two specific ARNs (the vector bucket and the index) and because the exact
> action set for a KB's read/write path against S3 Vectors isn't documented
> tightly. The right next step is to run an ingestion + retrieval, pull the
> actual calls from CloudTrail, and narrow the policy to them.
> **Senses:** interviewers aren't testing whether your IAM is perfect. They're
> testing whether you *know* where it isn't.
> *docpipe: `pulumi/components/kb.py:128-133`.*

**M5.4 — There is no NAT gateway. How does a Lambda in a private subnet call Bedrock?**
> It can't, without either NAT (~$32/mo idle) or a **PrivateLink interface VPC
> endpoint** for `bedrock-runtime` (hourly per-AZ + data processing). docpipe
> sidesteps it: the chat Lambda runs **outside** the VPC, the KB is serverless
> S3 Vectors, and Aurora is reached via the RDS Data API — so nothing needs VPC
> egress, and S3/DynamoDB use free gateway endpoints.
> **Follow-up:** "compliance now requires Bedrock traffic never traverse the
> public internet." Then you pay: Lambda into the VPC, interface endpoints for
> `bedrock-runtime` (and `bedrock-agent-runtime`), and you've just reintroduced
> cold-start ENI costs and a standing bill. Being able to price the compliance
> requirement is the answer they want.
> *docpipe: README cost notes.*

**M5.5 — Name the four standing-cost sinks in a stack like this and how you avoided each.**
> NAT gateway ~$32/mo → removed, no VPC egress needed. OpenSearch Serverless
> ~$345/mo floor → S3 Vectors instead. EKS control plane ~$73/mo just to exist →
> deferred, and Lambda is the default recommendation for an occasional summary
> job. Aurora warm cluster ~$43/mo+ → `min_capacity=0` auto-pause, gated off by
> default behind `enable_aurora`.
> **Follow-up:** what does `min_capacity=0` cost you? A resume latency on the
> first query after idle — accepted because the summary path is async.
> **Measured?** No — README carries an explicit TODO. Say "not yet measured."

**M5.6 — How do you cap Bedrock spend so a bug can't produce a $10k bill?**
> Layered, and name the layer each one catches: API Gateway **usage plan with a
> daily quota** (a hard request cap — the reason REST API Gateway was chosen over
> HTTP API), a **per-user token bucket in DynamoDB** in the Lambda (usage plans
> throttle per *key*, not per *user*), `maxTokens` on every Converse call, an AWS
> **Budgets** alarm on Bedrock spend, and a CloudWatch alarm on invocation-count
> spikes.
> **Trap:** Budgets alerts are lagging — hours, not seconds. They tell you about
> the bill, they don't prevent it. Only the quota and the token bucket are
> actually preventive.
> **Follow-up:** how do you make the DynamoDB token bucket correct under
> concurrency? Atomic `UpdateItem` with a conditional expression, not
> read-modify-write.
> *docpipe: `PLAN.md` Phase 3 auth/rate-limiting.*

**M5.7 — What do you log for a Bedrock call, and what must never be logged?**
> Log: model id, input/output token counts, latency, stop reason, guardrail
> intervention, request id, `userId` (opaque), conversation id. Emit metrics as
> **CloudWatch EMF** so counts/latency become metrics without a second API call.
> Never log: prompt/response content into application logs. That's what **model
> invocation logging** is for — S3 + CloudWatch, SSE, retention-capped, delivered
> under a Bedrock-assumed role — so the audit trail is access-controlled
> separately from your app logs.
> **Follow-up:** invocation logging captures full prompts and completions, and
> health content is sensitive. Who can read that bucket, and for how long?
> *docpipe: `observability.py`; `pulumi/components/safety.py:108-213`.*

**M5.8 — How do you unit-test all of this without spending a cent?**
> moto for S3/DynamoDB/SQS; dependency-injected fakes for the Bedrock clients
> (there is no moto for Converse); injected `sleep`/`clock` so retry and polling
> logic is tested without wall-clock time. Full sync + ingestion path is covered
> with moto S3 + a fake `bedrock-agent`.
> **Senses:** the injected clock is the detail that signals someone who has
> actually tested a poller.
> *docpipe: `kb_sync.py:202-303` (`sleep`/`clock` params), `packages/core/tests/`.*

---

## M6 — Safety & governance

**M6.1 — Your guardrail is deployed but not yet passed to any Converse call. What is it currently protecting?**
> Nothing. Be the one who says it first. The guardrail exists
> (`pjpeeu9hf68a`), the system prompt carries the non-diagnostic stance, but
> `guardrailConfig` is not on the Converse calls — it's an explicit open item.
> **Follow-up:** why is the system prompt insufficient? Because it's inside the
> context window and prompt injection can talk it around; the guardrail is
> enforced outside the model. That's the entire architectural argument for
> guardrails, and it's undercut until they're wired in.
> *docpipe: `PLAN.md` Phase 3 safety, unchecked item.*

**M6.2 — Your guardrail version is `DRAFT`. Ship it?**
> No. `DRAFT` moves under you — an edit changes production behaviour with no
> deploy and no rollback point. Production pins a numbered version; DRAFT is for
> iteration. Versioning the guardrail is also what makes "what policy was in
> force when this answer was produced?" answerable in an audit.
> *docpipe: `pulumi/components/safety.py:106`.*

**M6.3 — `PROMPT_ATTACK` is configured with `output_strength: NONE`. Bug or requirement?**
> Requirement — the filter is input-only and AWS rejects a non-NONE output
> strength. Knowing it's a platform constraint rather than an oversight is the
> signal.
> *docpipe: `pulumi/components/safety.py:80-82`.*

**M6.4 — Contextual grounding is set to 0.75 for GROUNDING and RELEVANCE. What is it scoring, and where does it misfire here?**
> It scores the model's response against the retrieved passages (grounding) and
> against the user's query (relevance), blocking below threshold — a runtime
> hallucination check. The misfire risk in docpipe is specific: with 97/383
> chunks unrated and 172 uncited, a well-grounded answer built on *framing
> prose* passes the grounding check while being weakly evidenced. **Grounding ≠
> evidence quality.** The guardrail checks the first; `min_evidence` checks the
> second. They are not substitutes.
> **Follow-up:** how would you tune 0.75? Measure the false-block rate on your
> golden set. Which needs a golden set. (Everything routes back to M3.)

**M6.5 — PII is set to ANONYMIZE, but the design says no PII ever reaches AWS. Why both?**
> Defence in depth: the contract is opaque IDs only, but the guardrail catches
> the case where a *user types their own name or phone number into the chat* —
> which no upstream contract can prevent. Anonymize rather than block so the
> conversation survives.
> **Follow-up:** does ANONYMIZE protect the invocation log? Check before
> asserting — the audit trail capturing raw prompts is exactly where PII would
> land, and "I'd verify that against a real invocation" is the correct answer.

**M6.6 — Your corpus deliberately excludes a document for a safety reason. Explain it — it's the most interesting thing in this repo.**
> `anatomy/referral-boundary.md` is excluded because **to forbid a dangerous
> sentence, the document must quote it — and a retrieved fragment cannot tell
> the quotation from the claim.** Retrieval strips the frame that made the quote
> safe. A naive `docs/**/*.md` glob would have uploaded it along with four other
> policy-excluded files (75 KB total, including 36 KB of self-declared uncited
> market research).
> **Senses:** this generalises far beyond docpipe — *any* corpus containing
> negative examples, red-team prompts, or "never say X" documentation is unsafe
> to chunk naively. If you can state that generalisation, it's the strongest
> single answer in this bank.
> *docpipe: `FINDINGS.md` 2026-08-13.*

**M6.7 — Prompt injection against a RAG assistant: where are the entry points?**
> User message; **retrieved passage content** (a poisoned document instructs the
> model); tool descriptions and tool results (M4.4); conversation history
> replayed from storage. Defences: guardrail `PROMPT_ATTACK` on input,
> instruction/data separation in the prompt, treating passages as quoted data,
> output-side guardrails, and corpus provenance control — which for docpipe is
> strong, since the corpus is a build artifact from a repo you control.

---

## M7 — System design scenarios

Run these as 45–60 minute open problems. Talk through the ladder; the follow-ups
are the interview.

**M7.1 — Design a grounded, non-diagnostic health assistant for 10,000 daily users at under $500/mo. Where does it break first?**
> Expect escalation on: per-request token math (system prompt + k passages +
> history × turns), which cost lever you pull first (batch doesn't apply — it's
> synchronous; caching does, but only on Claude/Nova), where you'd cap
> (usage-plan quota + per-user bucket), what breaks first (TPM quota, then
> DynamoDB hot partitions on a power user, then Lambda concurrency), and how you'd
> know (EMF metrics + a p95 latency alarm).

**M7.2 — The corpus grows from 383 chunks to 5 million. What changes?**
> Chunk-final stops being tenable (nobody hand-builds 5M chunks) → managed
> chunking, which reopens M2.2's header problem → the evidence legend must move
> into per-chunk metadata rather than prose. S3 Vectors still fits (2B/index).
> Retrieval quality degrades with corpus size → reranking and hybrid search
> start earning their cost. Ingestion moves from a CLI to an event-driven
> pipeline. And **Managed Knowledge Base becomes the right answer** — say so.

**M7.3 — Migrate this off Bedrock to another provider in one quarter. What hurts?**
> Cheap: Converse-based inference (docpipe is already model-agnostic by
> construction). Expensive: Knowledge Bases (retrieval API, ingestion, sidecar
> metadata semantics), Guardrails (no portable equivalent — you'd rebuild policy
> enforcement), invocation logging, IAM-based auth, and the S3 Vectors index.
> **Senses:** the point is recognising that the *managed* services are the lock-in
> and the *model* is not — which inverts the usual assumption, and is the reason
> docpipe's Converse-first design was worth the effort.

**M7.4 — An answer cited a passage that doesn't support it. Debug it.**
> Reproduce with the same query and log the retrieved passage set. Then bisect:
> was the right passage retrieved (retrieval failure → embeddings, k, filter) or
> retrieved-and-ignored (generation failure → prompt, model, grounding
> threshold)? Check whether the citation maps to a passage actually in context
> or was fabricated. Check whether contextual grounding scored it and passed.
> Then ask whether the golden set would have caught it — and add it if not.

---

## M8 — The docpipe cross-exam (hostile)

Answer these *before* an interviewer does. Each is a real gap; each has a real
answer of the form "yes, here's why, here's what I'd do."

1. **"Your guardrail doesn't do anything."** True today — provisioned, not
   wired. Phase 3 item. (M6.1)
2. **"You have no evaluation. How do you know any of this works?"** Half-stale
   since 2026-08-16 — correct them with numbers: retrieval *is* evaluated (the
   66-question ratified set, recall@5 0.79 → 0.82 under reranking, measured
   twice, M3.1). The half that's still true: **generation quality is
   unmeasured** — no faithfulness, citation-coverage, or refusal-rate numbers.
   Concede that half fast, with the M3.3 metric list as the plan.
3. **"You built a custom KB two months after AWS shipped the managed one."**
   (M2.1 — you have a genuinely good answer.)
4. **"Your retries have no jitter."** Fixed 2026-08-17 — `2**attempt` plus up
   to 1 s of uniform jitter, with the rng injected for tests. The stronger
   follow-up you can now volunteer: botocore's `adaptive` mode adds client-side
   rate limiting on top, which per-call jitter does not.
5. **"You never confirmed the sidecar fits the 2 KB metadata cap."** Correct —
   ≤1,705 B pretty-printed is a calculation, not a measurement. (M2.12)
6. **"Phase 5's compute is still undecided."** Yes: EKS's ~$73/mo control plane
   for an occasional summary job is the biggest standing cost in the design and
   the reason it's still open. Lambda is the default recommendation; the
   benchmark is the stronger portfolio artifact than EKS-for-its-own-sake.
7. **"You use `s3vectors:*`."** (M5.3)
8. **"README says EKS worker, PLAN says probably Lambda."** Documentation drift.
   Fix it before anyone reads both.
9. **"Your agent doesn't exist yet."** Correct — the chat path today is
   non-agentic RAG that works. Which invites M4.3: what does the agent buy you?
10. **"Aurora resume latency is a TODO in your README."** Yes — an explicit
    unmeasured number, flagged rather than guessed. That's the right way to
    carry an unknown, and worth saying so.

**The meta-answer for this whole round:** every item above is already written
down in `PLAN.md`, `FINDINGS.md`, or the README, as an open item or a measured
number with its evidence. A candidate whose project documents its own gaps —
including a retraction discipline for findings that turned out wrong — is
demonstrating engineering judgment more convincingly than one whose project has
no visible gaps at all.
