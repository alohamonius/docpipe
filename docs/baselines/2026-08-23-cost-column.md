# The $ column — S3 Vectors vs Aurora pgvector, measured rates, worked per-query

Every rate below is sourced; nothing is a guess. Sizes are measured on the live
585-chunk corpus (2026-08-23). Agentic token counts are from the 2026-08-20
n=66 runs and confirmed by the 2026-08-23 re-baseline (unguarded/verbatim runs
444–517k in per 66 questions — same ~$0.0055/question; guarded runs are
~20% cheaper, ~363–396k, because blocked questions stop early).

## Rates (us-east-1, retrieved 2026-08-23)

| item | rate | source |
|---|---|---|
| S3 Vectors storage | $0.06 / GB-mo | Price List API, `Vectors-TimedStorage-ByteHrs` |
| S3 Vectors put | $0.20 / GB | Price List API, `Vectors-Put-Bytes` |
| S3 Vectors query requests | $0.055 / 1,000 (Tier1 = Tier2) | Price List API, `Vectors-Request-Tier*` |
| S3 Vectors query processed | $0.000003906 / GB (Tier1) | Price List API |
| S3 Vectors query returned | $0.01 / GB | Price List API |
| Aurora Serverless v2 | $0.12 / ACU-hr; storage $0.10 / GB-mo; backup $0.021 / GB-mo; I/O $0.20 / M | Price List API, verified 2026-08-13 (PLAN) |
| DeepSeek V3.2 (Converse) | $0.62 / 1M in · $1.85 / 1M out | aws.amazon.com/bedrock/pricing, 2026-08-23 |
| Titan Text Embeddings V2 | $0.02 / 1M input tokens | pricing page; corroborated by the "~420k-token corpus ≈ 1¢" check |
| Cohere Rerank 3.5 | $0.002 / search unit (1 query ≤ 100 docs) | **Price List API `USE1-MP:USE1_search_units-Units` AND pricing page ($2.00/1k queries) — two sources agree** |

> **Correction to PLAN:** "the Price List API carries no rerank SKU" is no
> longer true — `Cohere Rerank v3.5 (Amazon Bedrock Edition)` is in
> `AmazonBedrockFoundationModels` as of 2026-08-23. DeepSeek and Titan remain
> absent from the Price List (page-sourced above); that service code lists
> third-party models only, and not all of them.

## At rest, per month

Measured sizes: Aurora database 21.3 MB total, vector table + 3 indexes
13.0 MB (`pg_total_relation_size`, 2026-08-23). S3 Vectors logical size ≈
585 × (4 KB vector + ≤2 KB metadata) ≈ 3.5 MB.

| | S3 Vectors | Aurora pgvector |
|---|---|---|
| storage | ~$0.0002 | ~$0.002 + ~$0.0004 backup |
| existence floor | $0 | **~$0.12** (min-ACU-0 pauses compute, not storage/backup) |
| **total** | **≈ $0.00** | **≈ $0.12** |

Full corpus (re)embed: ~420k tokens × $0.02/1M ≈ **$0.008**. Full ingest put
to S3 Vectors: ≈ $0.001. Both rounding errors.

## Per query, worked

Query embedding (~30 tokens Titan v2): $0.0000006 — ignored below.

**Single-shot retrieve, k=5:**
- S3 Vectors: 1 request $0.000055 + processed (2.4 MB × $3.9e-6/GB) + returned
  (~10 KB × $0.01/GB) ≈ **$0.000056** → $0.06 per 1,000 questions.
- Aurora, warm: ~0.5 s at 0.5 ACU ≈ **$0.000009** of ACU + ~0 I/O (13 MB table
  is cache-resident) → $0.01 per 1,000 questions.
- Aurora, cold: the query itself is not the cost — the **wake** is. Resume
  (~15 s) + the idle window before re-pause at 0.5 ACU ≈ **~$0.005 per
  isolated visit**, i.e. one cold question costs ~500 warm ones.

**+ rerank (25-doc pool = 1 search unit):** + **$0.002** per question — 36×
the S3 Vectors retrieve it sits on. At 3 req/min (account, not adjustable) it
is also the only rate-limited thing on the path.

**Agentic (DeepSeek loop, ~2.9 tool calls/question, n=66 runs):**
- DeepSeek tokens: ~485k in / ~35k out per 66 questions ≈ **$0.0055 per
  question** — identical both stores (store-agnostic loop, measured).
- store queries: S3 Vectors +$0.00016; Aurora warm +$0.000026.
- **≈ $0.006 per question either way.**

## The verdict the column supports

At this corpus size the stores are **not a cost decision any more than they
were a recall decision**: both are ≤ $0.12/mo at rest, and per-query store
cost is 1–3% of the agentic path's LLM cost. The real cost structure is:

1. **DeepSeek dominates** — the agentic loop is ~100× the retrieval it drives
   ($5.50 vs $0.06 per 1,000 questions).
2. **Rerank is the most expensive retrieval component** ($2/1,000), not the
   vector store.
3. Aurora's cost is shaped by **traffic pattern, not volume**: steady traffic
   amortizes the wake to ~$0.01/1k; sparse traffic pays ~$0.005 per cold
   visit. S3 Vectors is flat $0.06/1k regardless of pattern — the only regime
   where the store choice shows up in money at all.
