# Findings index

Generated from `FINDINGS.md` by `scripts/findings/index-md.mjs` — do not hand-edit.
Read this first; open the log only for the entry you need.

46 findings in 8 sections.

## Bedrock's platform limits <sub>(4)</sub>

What the managed service accepts, rejects and silently changed under us.

- 2026-08-13 — [Bedrock access day: model + platform facts](../../FINDINGS.md#2026-08-13--bedrock-access-day-model--platform-facts)
- 2026-08-13 — [the managed layer moved under us](../../FINDINGS.md#2026-08-13--the-managed-layer-moved-under-us)
- 2026-08-13 — [Bedrock KB rejects Cohere Embed v4, under every storage type](../../FINDINGS.md#2026-08-13--bedrock-kb-rejects-cohere-embed-v4-under-every-storage-type)
- 2026-08-13 — [an untracked Managed KB is live in the account](../../FINDINGS.md#2026-08-13--an-untracked-managed-kb-is-live-in-the-account)

## Aurora as a vector store <sub>(8)</sub>

Serverless v2 at min_capacity 0, its cold start, and how Bedrock scores it.

- 2026-08-13 — [Aurora cold-resume accepted as a UX problem, not a cost problem](../../FINDINGS.md#2026-08-13--aurora-cold-resume-accepted-as-a-ux-problem-not-a-cost-problem)
- 2026-08-13 — [Aurora at `min_capacity = 0` IS supported by Bedrock KB](../../FINDINGS.md#2026-08-13--aurora-at-min_capacity--0-is-supported-by-bedrock-kb)
- 2026-08-14 — [Aurora has a fifth gap, and the docs disagreed about whether it exists](../../FINDINGS.md#2026-08-14--aurora-has-a-fifth-gap-and-the-docs-disagreed-about-whether-it-exists)
- 2026-08-19 — [the case for Aurora consolidated: three product features, not one benchmark](../../FINDINGS.md#2026-08-19--the-case-for-aurora-consolidated-three-product-features-not-one-benchmark)
- 2026-08-20 — [first Aurora ingest: 496/496, and the two KBs no longer hold the same corpus](../../FINDINGS.md#2026-08-20--first-aurora-ingest-496496-and-the-two-kbs-no-longer-hold-the-same-corpus)
- 2026-08-20 — ["wtf with aurora" solved: Bedrock's RDS path returns cosine DISTANCE as score and sorts worst-first](../../FINDINGS.md#2026-08-20--wtf-with-aurora-solved-bedrocks-rds-path-returns-cosine-distance-as-score-and-sorts-worst-first)
- 2026-08-20 — [Aurora cold start on the Retrieve path: 1.32 s, not ~15 s](../../FINDINGS.md#2026-08-20--aurora-cold-start-on-the-retrieve-path-132-s-not-15-s)
- 2026-08-23 — [the ef_search sweep is a flat line: 585 vectors is too small for HNSW to approximate](../../FINDINGS.md#2026-08-23--the-ef_search-sweep-is-a-flat-line-585-vectors-is-too-small-for-hnsw-to-approximate)

## The corpus itself <sub>(11)</sub>

Chunking, growth, duplicates, and the graph hiding inside it.

- 2026-08-13 — [the corpus was already built, and we were about to ingest it wrong](../../FINDINGS.md#2026-08-13--the-corpus-was-already-built-and-we-were-about-to-ingest-it-wrong)
- 2026-08-13 — [the corpus size variance is one-sided, and it is the graph half](../../FINDINGS.md#2026-08-13--the-corpus-size-variance-is-one-sided-and-it-is-the-graph-half)
- 2026-08-14 — [the corpus grew a generation, and the chunking rule became a test](../../FINDINGS.md#2026-08-14--the-corpus-grew-a-generation-and-the-chunking-rule-became-a-test)
- 2026-08-14 — [the bibliography fix is unshippable as planned, and it is not urgent](../../FINDINGS.md#2026-08-14--the-bibliography-fix-is-unshippable-as-planned-and-it-is-not-urgent)
- 2026-08-14 — [the syncer could not delete, and the bucket is a measured zero](../../FINDINGS.md#2026-08-14--the-syncer-could-not-delete-and-the-bucket-is-a-measured-zero)
- 2026-08-16 — [Pre-ingest review: the corpus is clean, the destination is not](../../FINDINGS.md#2026-08-16--pre-ingest-review-the-corpus-is-clean-the-destination-is-not)
- 2026-08-19 — [corpus is now 496 chunks; 72 are bibliography-only and filterable](../../FINDINGS.md#2026-08-19--corpus-is-now-496-chunks-72-are-bibliography-only-and-filterable)
- 2026-08-21 — [the corpus was already a graph, so Graph RAG needed a parser, not a database — and Neptune priced at ~$350/mo floor](../../FINDINGS.md#2026-08-21--the-corpus-was-already-a-graph-so-graph-rag-needed-a-parser-not-a-database--and-neptune-priced-at-350mo-floor)
- 2026-08-21 — [the corpus outgrew the embedder while nobody was looking, and `plan` was an N+1 that hid behind a 2-file fixture](../../FINDINGS.md#2026-08-21--the-corpus-outgrew-the-embedder-while-nobody-was-looking-and-plan-was-an-n1-that-hid-behind-a-2-file-fixture)
- 2026-08-22 — [the first real sync landed 96/96, and printed two numbers wrong on the way](../../FINDINGS.md#2026-08-22--the-first-real-sync-landed-9696-and-printed-two-numbers-wrong-on-the-way)
- 2026-08-23 — [304 near-duplicate pairs, and every one of them is the graph registry talking to itself](../../FINDINGS.md#2026-08-23--304-near-duplicate-pairs-and-every-one-of-them-is-the-graph-registry-talking-to-itself)

## Measuring retrieval <sub>(9)</sub>

Recall and MRR, the rerank lever, and what each probe actually proved.

- 2026-08-13 — [three retrieval levers we provision nothing for](../../FINDINGS.md#2026-08-13--three-retrieval-levers-we-provision-nothing-for)
- 2026-08-16 — [the baseline: 0.7879, and the exit code is the corpus's known defect being measured, not a bug](../../FINDINGS.md#2026-08-16--the-baseline-07879-and-the-exit-code-is-the-corpuss-known-defect-being-measured-not-a-bug)
- 2026-08-16 — [miss-rank probe: 7 of the 9 baseline misses sit at rank 7–19](../../FINDINGS.md#2026-08-16--miss-rank-probe-7-of-the-9-baseline-misses-sit-at-rank-719)
- 2026-08-17 — [the reranker, measured: +0.1139 MRR, four rescues, and two questions it broke](../../FINDINGS.md#2026-08-17--the-reranker-measured-01139-mrr-four-rescues-and-two-questions-it-broke)
- 2026-08-17 — [`make kb-eval RERANK=--rerank` cannot run: 3 requests per minute, not adjustable](../../FINDINGS.md#2026-08-17--make-kb-eval-rerank--rerank-cannot-run-3-requests-per-minute-not-adjustable)
- 2026-08-20 — [first cross-store benchmark: same recall, Aurora semantic ranks far worse, hybrid buys the rank back](../../FINDINGS.md#2026-08-20--first-cross-store-benchmark-same-recall-aurora-semantic-ranks-far-worse-hybrid-buys-the-rank-back)
- 2026-08-22 — [the reingest doubled the graph and cost 4 questions of flat recall; every loss was already at the k=5 boundary](../../FINDINGS.md#2026-08-22--the-reingest-doubled-the-graph-and-cost-4-questions-of-flat-recall-every-loss-was-already-at-the-k5-boundary)
- 2026-08-23 — [with rerank granted, the two stores become the same instrument](../../FINDINGS.md#2026-08-23--with-rerank-granted-the-two-stores-become-the-same-instrument)
- 2026-08-23 — [graph expansion is dominated by `k=9`, and the anecdote that justified it was an argument for a bigger k](../../FINDINGS.md#2026-08-23--graph-expansion-is-dominated-by-k9-and-the-anecdote-that-justified-it-was-an-argument-for-a-bigger-k)

## The agentic loop <sub>(4)</sub>

What a multi-search tool loop does that a single shot cannot.

- 2026-08-18 — [V3.2 drives a multi-search tool loop unprompted: 3 retrievals, self-set evidence floor, end_turn at call 4](../../FINDINGS.md#2026-08-18--v32-drives-a-multi-search-tool-loop-unprompted-3-retrievals-self-set-evidence-floor-end_turn-at-call-4)
- 2026-08-20 — [agentic rows land: the loop beats every single-shot row, and Aurora agentic hits 0.9508](../../FINDINGS.md#2026-08-20--agentic-rows-land-the-loop-beats-every-single-shot-row-and-aurora-agentic-hits-09508)
- 2026-08-20 — [six agentic runs: the 0.9508 was a high roll, hybrid is flat, and the union hits 61/61](../../FINDINGS.md#2026-08-20--six-agentic-runs-the-09508-was-a-high-roll-hybrid-is-flat-and-the-union-hits-6161)
- 2026-08-21 — [building the playground found two live defects: single-shot never retried Aurora's auto-pause, and the agentic path applies no guardrail](../../FINDINGS.md#2026-08-21--building-the-playground-found-two-live-defects-single-shot-never-retried-auroras-auto-pause-and-the-agentic-path-applies-no-guardrail)

## The guardrail <sub>(3)</sub>

What it blocked, and what it turned out to be blocking instead.

- 2026-08-14 — [the guardrail was decoration, and GLM lost on grounds other than the numbers](../../FINDINGS.md#2026-08-14--the-guardrail-was-decoration-and-glm-lost-on-grounds-other-than-the-numbers)
- 2026-08-23 — [the guardrail costs ten points of recall exactly where the corpus is most clinical; the verbatim seed buys four back](../../FINDINGS.md#2026-08-23--the-guardrail-costs-ten-points-of-recall-exactly-where-the-corpus-is-most-clinical-the-verbatim-seed-buys-four-back)
- 2026-08-24 — [The guardrail was never blocking the questions. It was blocking the corpus.](../../FINDINGS.md#2026-08-24--the-guardrail-was-never-blocking-the-questions-it-was-blocking-the-corpus)

## Pulumi, state and drift <sub>(6)</sub>

The stack, its backend, and every time the written plan lagged the deployed one.

- 2026-08-13 — [Pulumi S3-backend + dynamic-provider gotchas](../../FINDINGS.md#2026-08-13--pulumi-s3-backend--dynamic-provider-gotchas)
- 2026-08-14 — [the app stays on Hetzner, and the VPC is not the seam](../../FINDINGS.md#2026-08-14--the-app-stays-on-hetzner-and-the-vpc-is-not-the-seam)
- 2026-08-17 — [the live stack is now asserted, and it was already 41 keys stale](../../FINDINGS.md#2026-08-17--the-live-stack-is-now-asserted-and-it-was-already-41-keys-stale)
- 2026-08-20 — [the three Aurora applies ran; four first-run bugs, all four now fixed in code](../../FINDINGS.md#2026-08-20--the-three-aurora-applies-ran-four-first-run-bugs-all-four-now-fixed-in-code)
- 2026-08-21 — [`pulumi/` → `infra/`: the rename is safe for state, and it exposed an isort misclassification that had been silently wrong all along](../../FINDINGS.md#2026-08-21--pulumi--infra-the-rename-is-safe-for-state-and-it-exposed-an-isort-misclassification-that-had-been-silently-wrong-all-along)
- 2026-08-21 — [PLAN's "locked decisions" had drifted 5 days behind the deployed stack, and the drift was invisible because the phase sections were right](../../FINDINGS.md#2026-08-21--plans-locked-decisions-had-drifted-5-days-behind-the-deployed-stack-and-the-drift-was-invisible-because-the-phase-sections-were-right)

## What it costs <sub>(1)</sub>

The marginal dollar, and which component actually owns it.

- 2026-08-23 — [the $ column: the store is 1–3% of the marginal cost, and the Price List now has a rerank SKU](../../FINDINGS.md#2026-08-23--the--column-the-store-is-13-of-the-marginal-cost-and-the-price-list-now-has-a-rerank-sku)
