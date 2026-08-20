# Drill log

Self-scores per [README.md](README.md): 0 can't answer / 1 textbook / 2 grounded.
Newest session last.

## 2026-08-16/17 — session 1 (Socratic, M0 → M2 + M3.1)

**M0 rapid fire: 10/20.** Q6, Q7 scored 2 (chunk-final, sidecars — the built
parts). Zeros on the four boto3 clients and RPM/TPM quotas; ones on Converse
rationale (had tools backwards — tools live *in* Converse via `toolConfig`),
inference profiles, batch inference (knew the idea, not the name), guardrail
vs system prompt ("enforced outside the model" was missing), dimension pinning.

**M1 (1.1–1.3, Socratic):** learned, not yet owned —
- Bedrock is stateless → passages re-paid every turn; caching covers only the
  byte-identical prefix. Initially inverted the conclusion: **system-placement
  kills cache** (changed bytes at top poison everything after), user-placement
  bloats history. No free lunch; the trade-off is the answer.
- Retry storm: spotted that synchronized retries refail ("exact same RPM") —
  good. Fix word: jitter. Ladder order internalized: jitter → measure →
  quota request → cross-region (already on via `us.` prefix — didn't know
  that) → PT last (and PT excludes inference profiles).
- ValidationException: got "useless to retry", needed the "harmful" half
  (burns user latency, hides a deploy bug as flaky noise).

**M2 (2.1–2.7, Socratic):** strongest module, as expected —
- Independently proposed moving the static evidence legend into the system
  prompt — which *is* the documented M7.2 scaling answer. Counter-arguments
  now known: per-chunk provenance can't move, file-level `maxEvidence`
  becomes dishonest on fragments, and splitting buys nothing today.
- "SEMANTIC is a better splitter; my problem is splitting at all" — landed.
- Believed oversized chunks fail *silently*; corrected: with `NONE` they fail
  loud at ingestion (the good kind) — but `be5fa22` shows COMPLETE + 0 failed
  + 0 vectors is possible, so always verify vector counts.
- Sidecar concept was shaky (asked "what is a sidecar?" mid-module) — fixed
  with the real `.metadata.json`; misread `includeForEmbedding` (maxEvidence
  is `false`; docTitle/section are `true`).
- Data-source replace: knew "new id" but thought re-ingest is automatic. It
  isn't: vectors purged, new source empty until `StartIngestionJob`.

**M3.1 (via the rerank work):** recall@5 / MRR / rank-1 vocabulary was new.
In the closing 60-second answer, conflated rank-1 hits (29/66) with top-k
recall (52/66) — the exact fumble to drill out. The reusable answer skeleton:
*set → baseline → hypothesis → measured delta → named costs.*

**Finding — the prep material is stale, in our favor.** `question-bank.md`
(M3.1, M8.2) still says "there is no golden set; zero evals — own it fast."
False since 2026-08-16: `retrieval.py:107-128` documents the 66-question
ratified set, baseline recall@5 0.7879 / MRR 0.55, rerank → 0.8182 / 0.68,
rank-1 29→39, a four-decimal control re-run, two named losses (biblio-01,
nerve-01), and the 3-RPM account-level rerank quota. Corpus numbers also
moved: **383 chunks / 97 unrated / 172 uncited**, not 285/99/180. Update the
question bank before the interview — quoting 285 would undercut the story.

**Next session:** M2 remainder (2.8 ETag/MD5, 2.10 vector-store choice with
numbers, 2.12 the 2 KB cap), M3.2–3.6, then M4 agents. Re-drill M0 cold —
target ≥16/20.

## 2026-08-17/18 — session 2 (Socratic, M2 close-out → M3.2 → M4 opening)

Side product first: the session's identified fixes shipped (retry jitter,
`OversizedDocRefused` pre-flight guard + `--max-doc-bytes`, prep-doc
de-staling) — see git history. Also measured: local build is now **496 docs +
496 sidecars** (live index still 383 — next sync ingests ~113 and the eval
should be re-run); largest sidecar 1,155 B; corpus total 4.8 MB ≈ **342k
tokens**.

**M2 close-out:**
- 2.8 ETag: needed prompting to the *direction-of-failure* point ("KMS →
  wasteful re-upload, never a false skip; failure direction beats failure
  rate"). Landed. Own-hash-in-object-metadata mitigation derived unprompted.
- 2.10 stores: 1.5 — picks right, numbers missing; didn't credit the losers.
  Key connection now owned: *biblio-01* is precisely the miss OpenSearch's
  BM25/hybrid would catch; Aurora's edge is hybrid-in-one-SQL + joins.
- 2.11 RAG-is-wrong: 1.5 → 2 with additions. Got aggregation + freshness +
  structured-query; missed fits-in-context (now grounded: 342k tokens = 1.7×
  a 200k window) and multi-hop (the sting: graph-connection stratum is the
  worst eval number, recall 0.60/MRR 0.30, and health.studio already has the
  code graph — RAG is the wrong tool exactly where docpipe measures weakest).
- 2.12: drilled the bounded-claim discipline — file size (1,155 B) is a proxy;
  Bedrock's own attributes share the 2 KB budget; the closer is naming the
  verification (`s3vectors list-vectors --return-metadata` on the
  fattest-sidecar chunk). Do NOT quote my illustrative "~100 B overhead" as
  fact — measure it.

**M3.2:** 1 — shape right (isolate case, check retrieval first), vagueness
corrected to the bisect-at-the-passage-boundary procedure: re-run `retrieve()`
alone; right chunk absent → retrieval (embeddings/k/filter/rerank, measurable
by recall@5); present → generation (prompt/model/grounding — invisible to the
current eval). One-liner: retrieval fails are measurable offline; generation
fails need a judge; end-to-end scores localise nothing.

**M4 opening:**
- Agentic RAG = `search_kb`-as-tool; the agent earns its cost on the measured
  misses (reformulation for biblio-01, `query_graph` for the 6 conn-* misses —
  cheaper and higher-impact than a hybrid-store migration for 2 misses).
- Candidate tool surface sketched: search_kb(query, min_evidence),
  history, get_source, check_red_flags, query_graph.
- **Misconception corrected: Converse is NOT "OpenAI spec"** — it's AWS's own
  uniform API; the OpenAI-compatible endpoint is a separate migration shim.
- M4.2 protocol: 0.5 cold, ~1/4 on fill-in-the-blanks. The gap: wire mechanics
  (toolConfig → stopReason tool_use → toolUse{name,input,toolUseId} → append
  assistant toolUse + user toolResult keyed by the same toolUseId → end_turn;
  bounds: max-iterations + token budget). Plan: write and RUN the naked
  40-line loop against V3.2 — hands-on, not flashcards.
- Model facts pinned: R1 = no tools, no cache; V3.2 = tools yes (own FINDINGS,
  ~400 tokens), cache no; caching = Claude/Nova only.

**Next session:** run the naked tool loop live; M3.3–3.6; M4.3–4.6; re-drill
M0 and M4.2 cold. Standing target: every answer with a number or a named
verification.
