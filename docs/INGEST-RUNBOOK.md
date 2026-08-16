# First ingest — runbook

**Written 2026-08-16.** Everything on the data side is finished; what remains is
one `pulumi up`, one sync, and one eval. This file is the handoff: state, the
command sequence, the number to check at each step, and what each failure means.

Evidence for every claim here is in `FINDINGS.md` (2026-08-16 entry) and
`health.studio/docs/FINDINGS.md`. Numbers were re-measured on the **merged**
corpus (post-06), not quoted from the pre-06 review.

---

## Where things stand

| | |
|---|---|
| Corpus | ✅ final — `health.studio` `main@cc146e2`, rebuilt, 383 chunks + 383 sidecars; `build/kb` verified current 2026-08-16, step 2 is a no-op |
| Dry run | ✅ rehearsed — `766 / 0`, with `AWS_PROFILE=docpipe` (see below) |
| Answer key | ✅ ratified — 66/66, `2026-08-16` by `obichuk` |
| Chunking `NONE` | ✅ applied 2026-08-16, verified `chunkingStrategy: NONE` |
| Index non-filterable keys | ✅ applied, verified `["AMAZON_BEDROCK_TEXT","AMAZON_BEDROCK_METADATA"]` |
| Live ids | KB `JDNNGSU1JT`, ds `U0PM4HIXGE` (both replaced; `SJQAFQXPH7` is dead) |
| Uploaded | ✅ 766 objects in `docpipe-dev-kb-source-733866507398` |
| Ingested | ❌ **blocked** — job `KWZPC25FGS` `COMPLETE` but 0 vectors: all 383 sidecars exceed the 1 KB metadata cap |
| Baseline | ❌ not taken — blocked on the above |

Both pending items are in the **same** `pulumi up`. Do not apply one alone —
see *Why both, together* below.

### The corpus, measured

```
383 chunks · 383 sidecars · 1,679,754 B · 418,397 tokens (cl100k proxy)
sidecar schema errors 0 · orphans 0 · docs without a sidecar 0
10 metadata attributes, uniform on every chunk, one type each
largest chunk 6,158 tok (referral-source--pancreas) = 75% of Titan v2's 8,192
evidence histogram  ★★★ 84 · ★★☆ 86 · ★☆☆ 69 · unrated 144
uncited 156 · safetyCritical 12
```

Re-audit any time: `python3 .scratch/kb_audit.py ../health.studio/build/kb`
(gitignored; promote to `scripts/` if it should run in the gate).

---

## Before you start — `AWS_PROFILE=docpipe` on steps 3 and 4

`CorpusSyncer` builds `boto3.client("s3")` with **no profile and no region**
(`packages/core/src/docpipe_core/kb_sync.py:161`), so `sync.py` inherits the
default credential chain. Measured 2026-08-16: the local `[default]` profile has
an invalid key (`InvalidClientTokenId`) and region `eu-west-1`, and step 3 run
verbatim dies with `ClientError: (403) … HeadObject: Forbidden` — *after* it has
printed the corpus summary, which makes it read like a corpus problem. It is not.

Steps 1 and 5 are unaffected and need nothing: Pulumi reads `aws:profile:
docpipe` from `Pulumi.dev.yaml`, and `kb_eval.py` hardcodes `PROFILE =
"docpipe"`. Only the two `sync.py` steps inherit, so only they need the prefix
(the profile carries `region = us-east-1`; `AWS_REGION` is not needed).

The durable fix is to give `CorpusSyncer` a `--profile` like `kb_eval.py` has —
deliberately **not** done before the baseline, because it is an unmeasured code
change on the ingest path.

## The sequence

### 1 · Apply — must be interactive

```bash
make infra-up          # exports the empty PULUMI_CONFIG_PASSPHRASE; confirm at the prompt
```

Expect `3 to update, 3 to replace`:

| resource | change |
|---|---|
| `pulumi-python:dynamic:Resource` (S3 Vectors index) | **replace** — gains `non_filterable_metadata_keys` |
| `aws:bedrock/agentKnowledgeBase` | **replace** — `indexArn` is `[unknown]` at plan time |
| `aws:bedrock/agentDataSource` | **replace** — `FIXED_SIZE(500, 20%)` → `NONE` |
| `worker-policy`, `api-lambda-policy` | update — pre-existing drift from `ff6c91f` |
| `kb-policy` | update — re-renders identically; it only shows as a diff because it depends on the replaced index ARN |

**The Knowledge Base id changes.** `SJQAFQXPH7` is dead after this. Nothing is
lost (0 vectors, 0 jobs), but re-read it rather than reusing it from `.env`,
`status.json`, or any note written before today:

```bash
cd pulumi && pulumi stack output --json | jq '{knowledge_base_id, data_source_id, kb_source_bucket}'
```

The IAM updates ride along because they are in the same stack: the `deepseek*`
wildcard narrows to two named models, and `bedrock:ApplyGuardrail` is added on
guardrail `pjpeeu9hf68a` — which is what makes the already-provisioned guardrail
enforceable instead of decorative. Ruled ship-it, human, 2026-08-16.

### 2 · Rebuild the corpus if it is not already current

`sync.py` never builds for you, and it refuses a `docs/` tree outright.

```bash
(cd ../health.studio && pnpm kb:build)
```

Expect `383 chunks` and `provenance gate: 0 unbacked ★ ratings, 63 ledgered`.
A non-zero unbacked count means the build is not from `main@cc146e2` or later.

### 3 · Dry run — no writes, no spend

```bash
AWS_PROFILE=docpipe uv run python services/kb_sync/sync.py --source ../health.studio/build/kb --dry-run
```

Expect `would upload: 766   unchanged (skipped): 0` — 383 documents + 383
sidecars. `766` is the number that says the sidecars are riding along; a bare
`383` means they are not, and retrieval would then have no evidence grades and
no named citations.

### 4 · Ingest

```bash
AWS_PROFILE=docpipe uv run python services/kb_sync/sync.py --source ../health.studio/build/kb
```

Ids resolve from `pulumi stack output` automatically, which is why this works
right after step 1 with only `--source` — and why you should **not** pass ids
from `.env`, which is stale as of step 1.

**`failed` is not the number that matters — `indexed` is.** Corrected
2026-08-16, after job `KWZPC25FGS` returned `COMPLETE` with `failed: 0`,
`skipped: 0` and **0 vectors written**. Bedrock *ignores* an oversized-sidecar
document: it counts in `Scanned`, in no other bucket, and appears only in
`failureReasons` — on a job whose status is `COMPLETE`. Every summary number
looks like success.

So check two things, not one:

```bash
AWS_PROFILE=docpipe uv run --with boto3 python - <<'PY'
import boto3, json
s = boto3.Session(profile_name="docpipe", region_name="us-east-1")
j = s.client("bedrock-agent").get_ingestion_job(
    knowledgeBaseId=KB, dataSourceId=DS, ingestionJobId=JOB)["ingestionJob"]
print(json.dumps(j["statistics"], indent=2), j.get("failureReasons"))
v = s.client("s3vectors")
print("vectors:", len(v.list_vectors(vectorBucketName="docpipe-dev-vectors",
                                     indexName="docpipe-dev-kb", maxResults=500)["vectors"]))
PY
```

Expect `numberOfNewDocumentsIndexed: 383` and a non-zero vector count. Anything
else, read this table before changing anything:

| symptom | almost certainly | fix |
|---|---|---|
| `ClientError (403) … HeadObject` before any count | `AWS_PROFILE=docpipe` is missing — see *Before you start* | re-run with the prefix; nothing was written |
| `COMPLETE`, `indexed 0`, `failed 0`, `failureReasons: Ignored 383 files … 1024 bytes` | **the sidecars exceed the 1 KB metadata cap** — hit for real 2026-08-16 | shrink the sidecar in `health.studio` `build-kb.ts`; minifying alone clears only 34 of 383. See `FINDINGS.md` for the priced variants |
| `failed: 383` | `STRING_LIST` is rejected on S3 Vectors — see *Open risk* | flatten `verification` to a comma-joined `STRING` in `stamp.ts`; nothing downstream filters on it |
| `failed: ~241` | the non-filterable keys did not take | `aws s3vectors get-index` (via boto3) and check `metadataConfiguration`; if absent, `_KEYS` did not fire |
| `failed: 0`, chunks look fragmented on retrieval | chunking is not `NONE` | `get_data_source` and check `chunkingStrategy` |

Re-running is safe and cheap either way: upload skips unchanged bytes (ETag ==
content MD5), and Bedrock re-embeds only what moved. The whole corpus is ~418k
tokens ≈ 1¢ at Titan v2 rates, so **cost is never a reason to avoid a
re-ingest.**

### 5 · Baseline

```bash
make kb-eval KB_ID=<the new id from step 1>
```

One `Retrieve` call per question (66), `k=5`, `min_evidence: null` — the baseline
measures the corpus, not a filter. The harness refuses an unratified key; it is
ratified, so it will score. `DRY=--dry-run` rehearses without recording.

Read `forbidden_violations` in the report, not only recall/MRR. It is hoisted to
the top level on purpose, and `gap-04` is the reason: two chunks outrank its key
lexically and say the reassuring inverse of the answer —
`body-graph-connection--scalenes` ("down the arm, where they can mimic cardiac";
its region list contains `left_arm`) and `05-muscle-anatomy-database--03-upper-body`
("arm (mimics cardiac pain)"). Neither is in `must_not_return` yet, deliberately:
`amend` on a `direct` card writes to `expected`, and they are not answers. If
`returned` shows them, that is the evidence for adding them — and
`forbidden_violations` cannot move recall or MRR, so adding them later does not
invalidate this baseline.

---

## Why both, together

The two pending changes mask each other, so applying either alone is worse than
applying neither.

- Bedrock stores the chunk body in the vector as `AMAZON_BEDROCK_TEXT`. S3
  Vectors caps **filterable** metadata at 2 KB per vector, and with no
  `metadataConfiguration` every key is filterable. **241 of 383 chunks exceed
  2,048 bytes** (p50 2,470 · p90 11,314 · max 24,747).
- `FIXED_SIZE` at 500 tokens produces fragments mostly under 2 KB, which is the
  only reason that cap has never been hit.
- So applying `NONE` alone trades a safety defect (the evidence legend and the
  "not a diagnosis" line are stripped from every fragment after the first) for a
  hard ingestion failure on ~63% of the corpus.

Non-filterable keys are declared at `CreateIndex` **or never** — the S3 Vectors
API (botocore 1.43.62) has `CreateIndex`, `DeleteIndex`, `GetIndex`,
`ListIndexes` and **no `UpdateIndex`**. Verified against the service model, not
assumed. Free to fix today; a delete-and-reingest after.

---

## Do not touch before the baseline

The baseline exists to measure exactly these. Changing one now means the delta
it was supposed to prove becomes unfalsifiable — the same reasoning behind the
human ruling of 2026-08-14.

- **Moving `Sources` into a sidecar attribute** (PLAN.md step 4). Also
  *impossible on S3 Vectors regardless*: the Bedrock integration caps **custom
  metadata at 1 KB per vector** and 35 keys ("you can attach up to 1KB of custom
  metadata and 35 metadata keys per vector" — S3 user guide, *Using S3 Vectors
  with Amazon Bedrock Knowledge Bases* → Limitations). ~~Current usage is 413 B,
  so ~600 B is free~~ — **wrong, corrected 2026-08-16: sidecars are 1,547–1,705 B
  and all 383 are already over the cap.** Nothing is free; this is the gate that
  blocked the first ingest, not a future ceiling. Declaring it non-filterable
  does **not** buy the 40 KB ceiling. This
  step survives on the Aurora/pgvector KB, which makes it a reason to build the
  second KB rather than something to do first.
- **Splitting oversized graph chunks** — renames keys; no rename hazard exists
  before an ingest anyway.
- **The shared header block** — 27.4% of the median chunk, >50% for 30 of them.
  Real cost, measured, and deliberately left in the control.
- **Widening `citationCount`** — 06 deferred this in writing for this exact
  reason: it moves the sidecar values being scored. 15 of the 96 original
  offenders are counter bugs, not corpus bugs (6 cite by bare `PMC…`/DOI that
  `URL_RE` misses, 2 carry a `SourceRef` with no URL, 7 *are* the ★ vocabulary).
- **17 citation collisions** — 41 chunks share a `(docId, section)` pair, so
  `RetrievedPassage.citation` renders identically for them (4 chunks all read
  *"Myofascial Chains → The 12 Myofascial Meridians"*). Producer-side, cosmetic,
  after.
- **Coverage** (67.7%) — a content timeline, never an ingest gate.

---

## Open risk, to be settled by the first job

**`STRING_LIST` on S3 Vectors.** All 383 sidecars carry `verification` as
`STRING_LIST`. Sources conflict: a practitioner write-up lists STRING_LIST among
the four supported Bedrock-KB types, while an AWS re:Post thread reports it
working on pgvector and **not** on S3 Vectors (that page 403s to automated
fetch — read it by hand if the job fails). Do **not** pre-emptively change it:
if it is rejected it fails all 383 loudly, and `verification` is the one
attribute no filter reads (`retrieval.py` filters only on `maxEvidence` and
`safetyCritical`), so flattening it costs nothing downstream. Cheaper to learn
than to guess.

---

## Settled — do not reopen without new measurements

- **Embedding model.** Titan v2 @ 1024 dims is forced, not chosen. Bedrock
  rejects Cohere Embed v4 for knowledge bases under every storage config
  (measured), and the only alternative family caps input at 512 tokens against a
  corpus reaching 6,158.
- **Chunking.** `NONE`. The corpus is chunk-final; any splitter at ingestion time
  strips the evidence legend. Splitting, where needed, happens upstream in the
  corpus build where headers can be re-stamped.
- **Near-duplicate dilution.** Measured, first time, 2026-08-16: 0 exact
  duplicate bodies, **0 pairs above 0.6** 5-gram Jaccard; 44 pairs in 0.5–0.6,
  all 19 of them `body-graph-structure--*`. The corpus is not diluted by
  duplication. This closed open question 6 in `docs/kb-data-status.md`.
