# Knowledge base — data status

**Date:** 2026-08-14 · **Corpus:** `health.studio/build/kb/` · **Target:** Bedrock
Knowledge Base `SJQAFQXPH7` (us-east-1)

Every number below was measured on the current build, not quoted from an earlier
report. Commands are in `FINDINGS.md` where they are non-obvious.

---

## Status at a glance

| | |
|---|---|
| Corpus built and valid | ✅ 383 chunks + 383 metadata sidecars, 1,686,867 B |
| Ingested into AWS | ❌ **nothing** — source bucket holds 0 objects, 0 ingestion jobs have run |
| Blocking defect | ⚠️ **1** — the live ingestion config is wrong (see *Blocker*) |
| Data quality issues | 5, none of which block launch |
| Embedding model | Locked: Titan v2 @ 1024 dims (forced — see *Closed questions*) |

**The corpus is shippable today.** Coverage is not, and never was, a gate on
ingestion: the corpus is self-describing and re-ingestion is just a rebuild, so
waiting for more content buys nothing. What would ship today is the muscle graph
at 99% completeness, which is the strongest asset in the repo.

---

## What the corpus contains

```
383 chunks   192 prose (from 20 source docs) · 191 graph
             244,301 words · 1,686,867 bytes
             every chunk carries a .metadata.json sidecar (9 attributes)

graph breakdown  132 connection · 31 structure · 11 chain · 8 nerve
                 5 referral-source · 2 entrapment · 1 joint · 1 index

evidence grades  ★★★ 93   ★★☆ 150   ★☆☆ 43   unrated 97
```

Chunks are **chunk-final**: each is one complete retrievable unit carrying its own
evidence legend, `[VERIFIED]` labels, and the "not a diagnosis" disclaimer inline.
12 chunks are flagged `safetyCritical` (all of `15-medical-red-flags`).

---

## Blocker — must be fixed before the first ingest

**The deployed ingestion config does not match the intended one.** The live data
source reports `FIXED_SIZE` (500 tokens, 20% overlap); the intended setting is
`NONE` (one chunk per file), which exists in code but has not been applied.

Ingesting as-is would split every chunk into fragments, and **only the first
fragment keeps the evidence legend and the medical disclaimer.** Every fragment
after it reaches the model as bare ★ ratings with no scale attached, on health
content. This is a correctness and safety defect, not a tuning preference.

Fix is a config change plus a redeploy. Note that applying it **replaces** the
data source, so its identifier changes.

---

## Data quality issues, ranked

None of these block launch. They are ranked by cost-of-delay, not severity.

### 1. One fifth of the corpus is bibliography — and it is being embedded

```
48,625 of 244,301 words (19.9%) sit inside a "Sources" section
160 of 383 chunks contain one
```

In the largest graph chunks it approaches half the text:

| chunk | bibliography | share |
|---|---|---|
| `referral-source--pancreas` | 1,832w of 3,692w | 50% |
| `referral-source--paranasal_sinus` | 1,488w of 2,626w | 57% |
| `referral-source--gallbladder` | 1,295w of 2,991w | 43% |
| `referral-source--uterus_prostate` | 1,222w of 2,435w | 50% |

Each chunk becomes **one vector**. A vector built half from author names and
journal titles is pulled away from the clinical content it is supposed to
represent — so a chunk whose bibliography outweighs its prose retrieves worse for
its own subject matter.

The citations are valuable and must be kept. The question is whether they belong
*in the embedded text* or alongside it.

**Cost of delay:** low to fix now, moderate later (changes chunk content → re-embed).

### 2. Graph chunks are 34× more variable in size than prose chunks

```
prose  n=192   median  264w   max   664w    ← healthy, 6× spread
graph  n=191   median  652w   max 3,692w    ← 34× spread
19 chunks exceed 2,000 words — all of them graph
```

Same root cause as above: one generated node becomes one file regardless of how
much that node knows. A 3,692-word chunk gets the same representational budget as
a 107-word one, so specific questions lose to shorter, more focused chunks.

Sectioning is already present and usable — the large files carry `###` headings
that separate genuinely different content:

```
pancreas (3,692w)   Mechanism 1,491w · Sources 1,832w · rest ~370w
sciatic  (2,435w)   Groups with a documented connection 1,978w · rest ~460w
```

So most of the fix is a heading-level split, with a second pass for the few
sections that are still oversized afterwards.

**Cost of delay: this is the only item with a deadline.** Splitting changes
filenames, which changes storage keys. Done before the first ingest it is free.
Done after, every renamed chunk leaves an obsolete entry behind that retrieves as
current text, and cleanup tooling has to be written to remove them.

### 3. Coverage is 67.7%, and the gaps are invisible rather than reported

```
Registry           Graded  Declined  Named  Missing  Blocked   Resolved
Muscles               159        13      3        1        0   175/176   99.4%
Nerves                  8         0      0       44        0     8/52    15.4%
Joints                  1         0      0       27        0     1/28     3.6%
Entrapment sites        2         0      0       14        0     2/16    12.5%
Referral sources        5         0      0        3        2     5/10    50.0%
Total                 175        13      3       89        2   191/282   67.7%
```

Muscles are effectively complete. The other four registries are collectively
**16 of 106**.

The important property is the failure mode, not the percentage. A missing row has
no node, so it is absent from the corpus rather than reported as excluded — the
export hold-back list comes back **empty**, because everything that *is* in a
registry is cited. Asked about one of the 44 missing nerves, retrieval will not
report a gap; it will return the nearest thing it does have.

**Cost of delay:** none for ingestion. This is a content-production timeline, and
re-ingestion is cheap enough to run as often as new rows land.

### 4. 45% of chunks cite no source literature

```
172 of 383 chunks have citationCount = 0
distribution: 0→172  1→53  2→39  3→22  4→12  5→23  6→16  7→14  8+→32
```

Concentrated in whole documents rather than scattered:
`04-neuromuscular-reeducation` 12/12 uncited, `02-nerve-pathways-and-crosstalk`
10/10, `01-myofascial-chains` 9/9, `05-muscle-anatomy-database` 14/15.

This is not lost data — that content genuinely carries no references. Framing
prose and conceptual models are legitimate, but **they retrieve identically to a
systematic review unless something separates them**, and on health content that
is the material risk in this project.

The mechanism to separate them already exists and is currently unused:
`citationCount` and `maxEvidence` are per-chunk attributes, so retrieval can
filter or label by them. Filtering to `citationCount >= 1` would leave 211 chunks.

**Decision needed** — see below.

### 5. 31 chunks display an evidence rating with nothing behind it

Every `graph-structure` chunk renders a ★ rating and has no Sources section,
because the underlying data type has no source field at all. The evidence trail
exists upstream in review files and never reaches the corpus.

Verified on `body-graph-structure--capitate`: renders `★☆☆`, sidecar reports
`maxEvidence: 1`, `verification: ["NONE"]`, `citationCount: 0`.

This is the exact failure the corpus builder's own documentation says it exists to
prevent — handing the model bare stars that read as authority. Harm is currently
low because those chunks carry little prose, but a rating with nothing behind it
converts "we did not record this" into "we verified this."

**Fix:** either carry the sources through, or drop the star. Unlike items 1–2 this
is a defect in the exporter, not in the data.

### 6. Not yet measured: are the 383 chunks actually distinct?

Near-duplicate detection has never been run. If a meaningful number of chunks
restate each other, that is dilution paid for on every query. This cannot be asked
through the retrieval API but is a single query once vectors exist in a SQL-backed
store.

---

## Recommended path

**Ship a minimal correct ingest, then iterate.** Only the blocker is mandatory.

| Step | Blocking? | Why this order |
|---|---|---|
| 1. Fix the ingestion config, redeploy | **Yes** | Correctness + safety defect |
| 2. Split oversized graph chunks; handle Sources | No, but time-sensitive | Free before first ingest, costly after |
| 3. Fix or drop the 31 unbacked ratings | No | Same rebuild as step 2 — no extra cost |
| 4. First ingest | — | |
| 5. Decide evidence/citation policy | No | Can be changed without re-ingesting |
| 6. Measure: duplicates, retrieval quality | No | Requires vectors to exist |

Steps 2–3 are content-side work costing nothing but time. Everything from step 5
onward is tunable after launch without rebuilding the corpus.

**Re-ingestion cost is negligible** — the full corpus is roughly 420,000 tokens,
about one cent to re-embed completely, and updates are incremental. Cadence should
be driven by how often content changes, never by cost.

---

## Decisions needed from a human

1. **Evidence policy.** When retrieval returns an unrated or uncited passage,
   should the system (a) refuse to use it, (b) use it but state that its
   provenance is unstated, or (c) use it silently? Recommendation: (b) —
   consistent with how the corpus already labels itself, and it does not discard
   45% of the content.
2. **Bibliography handling.** Keep citations inside the embedded text, or carry
   them alongside the chunk so they remain visible to users without diluting
   retrieval? Recommendation: alongside.
3. **The 31 unbacked ratings.** Carry sources through from the review files, or
   remove the rating until they are? Recommendation: remove the rating — it is
   faster and fails safe.
4. **Coverage priority.** Nerves (44 missing) and joints (27 missing) are the
   largest gaps. Which registry should content work target first?

---

## Closed questions

- **Embedding model is settled and forced, not chosen.** Bedrock rejects Cohere
  Embed v4 for knowledge bases under every storage configuration (measured). The
  only alternative family caps input at 512 tokens against a corpus reaching
  ~2,200 tokens per chunk, so it would truncate most of the graph half. Titan v2
  at 1,024 dimensions is the only viable option. Do not reopen without measuring.
- **Chunking strategy is settled.** The corpus is chunk-final; any splitter
  applied at ingestion time strips the evidence legend. Splitting, where needed,
  happens upstream in the corpus build where headers can be re-stamped.
