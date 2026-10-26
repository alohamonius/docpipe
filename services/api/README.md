# services/api

The Lambda behind **REST API Gateway** — `POST /chat`, the front door the root
README has drawn since August. Thin by rule: `chat.py` parses the proxy event,
runs one `docpipe_core.chat.ChatService.turn`, and shapes the response. All the
logic — history, the `KbAgent` tool loop, persistence, citations — lives in
`packages/core` with its tests.

## Contract

```http
POST /chat
x-api-key: <the usage plan's key>
content-type: application/json

{"userId": "<opaque id>", "message": "…", "conversationId": "<optional>"}
```

```json
{
  "reply": "…",
  "citations": [
    {"source_path": "…", "doc_title": "…", "section": "…", "max_evidence": 2,
     "verification": ["…"], "label": "Pain Science → Central sensitization (★★☆)"}
  ],
  "conversationId": "…",
  "model_id": "deepseek.v3.2",
  "guardrail_intervened": false,
  "latency_ms": 9800, "input_tokens": 4100, "output_tokens": 310, "tool_calls": 2
}
```

Omit `conversationId` to start a conversation; send it back to continue one.
`citations` are the passages the turn retrieved (deduplicated by `source_path`),
each with the five stamp fields health.studio's verifier reads. A reply that the
corpus cannot ground opens with `Not covered by the knowledge base.` — a prompt
rule the smoke test asserts, not a hope.

| status | meaning |
|---|---|
| 200 | the turn |
| 400 | body is not JSON, or missing `userId` / empty `message`; the detail names the field |
| 429 | from the gateway — usage-plan quota or method throttle; this function never ran |
| 502 | Bedrock failed after retries, or the loop ended without an answer; **the conversation is unchanged** |

## Configuration (set by `infra/components/api.py` on the function)

| variable | source |
|---|---|
| `KNOWLEDGE_BASE_ID` | stack `knowledge_base_id` (the S3 Vectors KB — no cold start) |
| `CONVERSATIONS_TABLE` | stack `conversations_table` |
| `CHAT_MODEL_ID` | stack `chat_model_id` |
| `GUARDRAIL_ID`, `GUARDRAIL_VERSION` | stack `guardrail_id` / `guardrail_version` |
| `CHAT_MAX_ITERATIONS`, `CHAT_TOP_K`, `CHAT_HISTORY_LIMIT`, `CONVERSATION_TTL_DAYS` | optional; defaults in `docpipe_core.chat` / `conversations` |

The role is the stack's `lambda_role_arn` (`infra/components/iam.py`): Retrieve
on the KB, InvokeModel on the chat model, ApplyGuardrail, and the conversations
table. Nothing here reads `.env`.

## Packaging and deploy

```bash
make lambda-zip        # build/lambda.zip: docpipe_core + chat.py + locked deps, arm64 wheels
make infra-preview     # the api component shows up as Lambda + REST API + usage plan + key
make infra-up          # (human) applies; `pulumi stack output chat_api_url` is the URL
```

The key's **value** is never exported or logged — read it from the console or
`pulumi stack output --show-secrets chat_api_key_value`… which does not exist on
purpose; use `aws apigateway get-api-key --api-key <chat_api_key_id> --include-value`.

## Smoke

```bash
DOCPIPE_API_URL=… DOCPIPE_API_KEY=… uv run python scripts/chat_smoke.py   # --json out.json
```

Two turns on one synthetic `userId` (`smoke-[a-f0-9]{8}`, enforced — this repo
is public), then an out-of-corpus question that must answer "not covered".
