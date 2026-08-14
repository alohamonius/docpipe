# Bedrock / GenAI-platform interview — the plan

You are the candidate. This is drill material: a syllabus that doubles as a
realistic loop, built so that **the answers are things docpipe actually does**.
Almost every hard question below has a real answer in this repo — a file, a
line, a number in [`FINDINGS.md`](../../FINDINGS.md). That is the point. Generic
Bedrock trivia is cheap and interviewers know it; a candidate who says
"chunking is disabled because the corpus is chunk-final and every chunk carries
its own evidence legend in a header a splitter would cut off" is not reciting.

- [`question-bank.md`](question-bank.md) — the questions, with what a strong
  answer contains, the traps, and `file:line` for the docpipe answer.
- [`landscape-2026.md`](landscape-2026.md) — what actually shipped and when.
  Bedrock moved a lot in the last 12 months; half of what's written online is
  about a product that is now in maintenance mode.

## The loop this simulates

A realistic senior/staff GenAI-platform loop is 4–5 rounds. The eight modules
below map onto them; the module numbers are a study order, not a schedule.

| Round | Modules | What is actually being tested |
|---|---|---|
| Phone screen (30m) | M0, M1 | Do you know the platform, or just the marketing page? |
| RAG deep-dive (45–60m) | M2, M3 | Can you make retrieval *good*, and can you prove it? |
| Agents (45m) | M4 | Do you know when **not** to build an agent? |
| Platform (45–60m) | M5, M6 | Would your thing survive a production quarter and an audit? |
| System design (60m) | M7 | Can you reason about cost and failure under a real constraint? |
| Project cross-exam (30–45m) | M8 | Can you defend your own decisions under pressure? |

**M8 is the round that gets people offers or sinks them.** You brought a
project; a good interviewer will spend the whole slot attacking it. M8 is
written hostile on purpose — it lists docpipe's *actual* soft spots (the
guardrail that's provisioned but not yet wired in, the zero evals, the
unverified 2 KB sidecar limit) before someone else finds them. Knowing your own
weak spots and having a plan for each reads as senior. Being surprised by them
does not.

## The three failure modes this is designed to prevent

1. **Recency rot.** Answering "Bedrock Agents" in 2026 — a product closed to new
   customers — is a hard signal you last touched this in 2024. M1 and
   `landscape-2026.md` fix this.
2. **No numbers.** "S3 Vectors is cheaper" is a vibe. "OpenSearch Serverless has
   a ~$345/mo floor, S3 Vectors is ~$0 at rest, and here is the retrieval
   latency I measured on the same corpus" is an engineer. Every cost and
   latency claim you make should have a number or an explicit "I haven't
   measured that yet."
3. **No evaluation story.** The single most common gap in RAG candidates. If you
   cannot answer "how do you know retrieval got better?" the rest doesn't
   matter. M3 exists because this is where most people fail, and — be honest —
   it is currently docpipe's biggest genuine hole (Phase 5b is unstarted).

## How to drill

Work one module at a time. For each question: **answer out loud, cold, before
reading the notes.** Reading answers you already half-know produces confidence
without recall — the failure mode of every flashcard app.

Then grade yourself on three things, in this order:

- **Correct?** Facts and mechanism.
- **Concrete?** A number, a file, a measured result, a specific API. An answer
  with no specifics is a fail even when it's technically true.
- **Bounded?** Did you say what you *don't* know, and what you'd do to find out?
  "I haven't confirmed the sidecar fits under the 2 KB filterable-metadata cap
  against a real ingestion — that's a `head-object` and a retrieve away" beats
  a confident guess, and interviewers score it that way.

Anything you get wrong, or any answer you had to invent on the spot, goes into
[`FINDINGS.md`](../../FINDINGS.md) with its evidence — same rule as the rest of
this repo. A finding that lives only in your head is lost.

## Marking your own tape

Three honest self-scores per module, tracked over time — not a rubric to
optimise, just a way to see which module you keep avoiding.

- **0 — can't answer.** Say it, move on, come back.
- **1 — textbook.** Correct but generic; no number, no file, no scar tissue.
- **2 — grounded.** Correct, specific, with the trade-off and the thing you'd
  check next.

You are ready for the round when every question in it is a 2 **and** you can
name the strongest argument *against* your own answer. Interviewers rarely ask
what you know; they ask what you'd do when the thing you know stops being true.
