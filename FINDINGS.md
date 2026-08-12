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
