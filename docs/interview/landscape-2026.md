# The Bedrock landscape as of August 2026

Checked 2026-08-13. Bedrock's shape changed twice in 18 months, and most
tutorials online describe a product that is now in maintenance mode. Knowing
*what replaced what, and when* is itself an interview signal — it proves you
have been near the platform recently rather than reading a 2024 blog post.

Dates marked ✅ were verified against an `aws.amazon.com` page or the AWS blog
during this research pass. Dates marked ~ came from secondary coverage and are
close but worth re-checking before you quote them in a room.

## The three things that changed under docpipe

### 1. Agents Classic → AgentCore

| When | What |
|---|---|
| ~Jul 2025 | AgentCore announced in preview |
| ~Oct 2025 | AgentCore GA — Runtime, Memory, Gateway, Identity, Browser, Code Interpreter, Observability |
| ~Dec 2025 | AgentCore **Policy** and **Evaluations** added (preview) |
| ~Jul 30 2026 | **Bedrock Agents "Classic" enters maintenance / closed to new customers** ✅ (see [`FINDINGS.md`](../../FINDINGS.md)) |
| **2026-06-18** ✅ | **AgentCore Harness GA** — `CreateHarness` + `InvokeHarness`, two calls to a production agent |

The Harness is the part worth understanding, because it changes the build-vs-buy
line. It wraps Runtime/Memory/Gateway/Identity/Browser/Observability as *managed
configuration* rather than wiring you assemble: "the harness handles that wiring
as a managed abstraction, so it becomes something you configure rather than
something you build." Omitting memory on `CreateHarness` provisions managed
memory automatically. Critically for docpipe's locked decision — **one CLI
command exports a harness as Strands-based code**, so harness-first is not a
one-way door, and neither is Strands-first.

**Why this matters to you:** docpipe's PLAN locked "Strands SDK in the API
Lambda" partly because Agents Classic was closed. That reasoning is still valid
but no longer complete — the honest 2026 answer is "Classic is closed;
AgentCore Harness is the managed option; I chose Strands-in-Lambda because
[latency / cost / the agent is three tools and a system prompt / I already pay
for a Lambda], and the export path means I can move later." Have that sentence
ready.

### 2. Custom Knowledge Base → Managed Knowledge Base

**2026-06-17** ✅ — **Amazon Bedrock Managed Knowledge Base** GA at the AWS New
York Summit. A fully-managed RAG service that collapses six components —
ingestion connectors, multimodal parsing, chunking, embeddings, vector store,
re-ranking — into a single API primitive. Six native connectors (S3, SharePoint,
Confluence, Google Drive, OneDrive, Web Crawler), managed vector storage, hybrid
search, document ranking, and *agentic retrieval* that orchestrates query
planning, interim response evaluation and re-ranking for multi-hop queries.

**This is now the default answer to "build a RAG pipeline on AWS."** docpipe
does not use it — `pulumi/components/kb.py` builds the custom KB with a
bring-your-own S3 Vectors index. You will be asked why. Your answer is in M2 of
the question bank, and it is a good one: docpipe's value is precisely in the
layers Managed KB takes over.

### 3. S3 Vectors grew up

| When | What |
|---|---|
| ~Jul 2025 | Preview, 5 regions |
| ~Dec 2025 | **GA** — 40× the preview scale, up to **2 billion vectors per index**, 14 regions |
| ~Mar 2026 | +17 regions → 31 total |

Positioned as a "storage-first" architecture that decouples compute from
storage; the claim is up to ~90% lower TCO for large-scale RAG. For docpipe the
relevant fact is the *floor*, not the ceiling: near-zero cost at rest against
OpenSearch Serverless's standing minimum, which is what makes the chat path
permanently deployable (README cost table).

## Inference economics — the whole M1 round in one table

| Lever | State | The catch that makes it an interview question |
|---|---|---|
| **Prompt caching** | GA; **Claude + Nova only** as of ~Jan 2026 (Nova 2 "soon"). Up to ~90% cost / ~85% latency reduction on cache hits. | Works via Converse/ConverseStream (multi-turn) and InvokeModel (single-turn). **DeepSeek is not on the supported list** — docpipe's model choice forfeits it, and docpipe re-sends a grounded system prompt with 4 passages every turn. That is a real, quantifiable trade-off. |
| **Intelligent Prompt Routing** | GA ~Apr 2025. Routes between two models **within one family** (Claude, Llama, Nova). | Real-time only — **no documented batch support**. Cross-family routing is your problem, not the router's. |
| **Batch inference** | ~50% discount for async work. | Only works if the workload tolerates the latency. docpipe's summary path is a textbook fit and doesn't use it — a fair thing to be asked. |
| **Model distillation** | GA. Teacher → smaller student on your traffic. | Needs real traffic and an eval set to prove the student didn't get worse. Which is M3 again. |
| **Cross-region inference profiles** | GA ~Aug 2024; **global** profiles later. Up to **2× in-region on-demand quota**. | The `us.` prefix (`us.deepseek.r1-v1:0`) *is* the profile. Global > geo > single-region for throughput. Quota ratios are deliberately capped. |
| **Provisioned Throughput** | Committed capacity, thousands $/mo. | **PT does not work through inference profiles** — you pick one or the other. Rule of thumb: only past ~30% sustained utilisation of your on-demand TPM. |

Two quotas govern on-demand per model per region: **RPM and TPM**. Crossing
either returns `ThrottlingException` / HTTP 429. Order of response: retry with
backoff+jitter → quota increase sized from *measured* peak → cross-region
profile → PT. Reaching for PT first is a red flag.

## Evaluation — GA, and still the round people fail

- **~Mar 20 2025** — RAG evaluation and **LLM-as-a-judge** GA in Bedrock, incl.
  evaluation *of Knowledge Bases* (retrieval quality and generation quality
  separately). **Citation coverage** and **citation precision** metrics added
  later — directly relevant to a citations-required assistant like docpipe.
- Retrieval metrics you should be able to name unprompted: recall@k, MRR,
  nDCG. Generation: faithfulness/groundedness, answer relevance, citation
  precision.
- Ecosystem: `ragas`, `deepeval`, `promptfoo` for offline scoring; the pattern
  that impresses is **evals as a CI quality gate** on a golden set, not a
  one-off notebook.

## Safety & governance

- **Guardrails** are independent of the model and apply to Converse/Invoke via
  `guardrailConfig`, and to retrieval flows. Policies: topic denial, content
  filters, PII (block/anonymize), word filters, **contextual grounding**
  (grounding + relevance thresholds — a runtime hallucination check scoring the
  answer against the retrieved passages).
- **Automated Reasoning checks** — formal/mathematical verification of model
  output against an encoded policy, rather than another probabilistic model
  judging it. Announced re:Invent 2024, GA in 2025. This is the differentiated
  one: if you can explain *why* a sound formal check beats an LLM judge for
  regulated claims, you are in a small minority of candidates.
- **Model invocation logging** → S3 + CloudWatch, account-level configuration
  (docpipe sets it via a boto3 dynamic provider because pulumi-aws has no
  resource for it — `pulumi/components/invocation_logging.py`).

## Agent frameworks — the honest comparison

| | Strands Agents SDK | AgentCore Harness | LangGraph | Claude Agent SDK |
|---|---|---|---|---|
| Who runs it | You (Lambda/Fargate/EKS) | AWS | You | You |
| Model coupling | Model-agnostic; native Bedrock + AgentCore | Bedrock | Any | Claude |
| Shape | Model-driven loop: model + tools + prompt | Two API calls, config not code | Explicit graph, you own control flow | Agentic loop + tool runner |
| Multi-agent | agent-as-tool, swarms | built-in | graph composition | subagents |
| MCP | yes | Gateway turns APIs into MCP tools | yes | yes |
| Escape hatch | it's your code | CLI export → Strands code | it's your code | it's your code |

Strands: preview May 2025, **1.0 ~Jul 2025**, Python **and** TypeScript, used in
production inside AWS (Amazon Q Developer, AWS Glue, VPC Reachability Analyzer).
The interview-grade point is not which is "best" — it's that **LangGraph gives
you explicit control flow and Strands gives you a model-driven loop**, and you
should pick based on whether your task needs auditable determinism or
flexibility.

## Models, briefly

- **Nova 2 Lite** GA ~2025-12-02 (multimodal, 1M context, 64K output); **Nova 2
  Sonic** (bidirectional speech) and **Nova 2 Multimodal Embeddings** early 2026.
- **Claude Opus 5** on Bedrock ~2026-07-24.
- 100+ models, 18+ providers, across text/image/video/speech/embeddings.
- docpipe's own measured facts (from [`FINDINGS.md`](../../FINDINGS.md), and
  better than anything you'll read online because you ran it): **DeepSeek V3.2
  does tool use via Converse**, is `ON_DEMAND`, invoked by plain model id, **no
  inference profile**; **DeepSeek R1 has no tool use** and needs the
  `us.deepseek.r1-v1:0` profile.

## Sources

- [Amazon Bedrock AgentCore harness is now generally available](https://aws.amazon.com/blogs/machine-learning/amazon-bedrock-agentcore-harness-is-now-generally-available-go-from-idea-to-production-grade-agent-in-minutes/)
- [Amazon Bedrock AgentCore is now generally available](https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-available)
- [AgentCore now includes Policy (preview), Evaluations (preview) and more](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-bedrock-agentcore-policy-evaluations-preview)
- [Amazon Bedrock Managed Knowledge Base is now generally available](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-managed-knowledge-base/)
- [Knowledge Bases now delivers a fully managed RAG experience](https://aws.amazon.com/blogs/aws/knowledge-bases-now-delivers-fully-managed-rag-experience-in-amazon-bedrock)
- [Amazon S3 Vectors is now generally available with 40× the scale of preview](https://aws.amazon.com/about-aws/whats-new/2025/12/amazon-s3-vectors-generally-available/)
- [Amazon S3 Vectors expands to 17 additional AWS Regions](https://aws.amazon.com/about-aws/whats-new/2026/03/s3-vectors-expands-17-regions)
- [New RAG evaluation and LLM-as-a-judge capabilities in Amazon Bedrock](https://aws.amazon.com/blogs/aws/new-rag-evaluation-and-llm-as-a-judge-capabilities-in-amazon-bedrock/)
- [Bedrock Knowledge Bases GraphRAG is now generally available](https://aws.amazon.com/about-aws/whats-new/2025/03/amazon-bedrock-knowledge-bases-graphrag-generally-available)
- [Amazon Bedrock Intelligent Prompt Routing is now generally available](https://aws.amazon.com/about-aws/whats-new/2025/04/amazon-bedrock-intelligent-prompt-routing-generally-available)
- [Increase throughput with cross-Region inference](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html)
- [Introducing Strands Agents 1.0](https://aws.amazon.com/blogs/opensource/introducing-strands-agents-1-0-production-ready-multi-agent-orchestration-made-simple/)
- [Open Protocols with the Strands Agents SDK](https://aws.amazon.com/blogs/opensource/open-protocols-with-the-strands-agents-sdk/)
- [Bedrock cost optimization](https://aws.amazon.com/bedrock/cost-optimization)
