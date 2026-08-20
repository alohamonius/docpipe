# services/worker

Async SQS consumer. Built in **Phase 5** — a declared stub, not yet code.

Polls the jobs queue, calls Bedrock (DeepSeek) via `docpipe-core`, writes the
summary to DynamoDB and job history to Aurora.

**Compute is an open decision — do not assume EKS.** PLAN.md Phase 5 leaves it
between Lambda, Fargate and EKS, with **Lambda the default recommendation**
(SQS event source, 15-min timeout covers LLM summaries, ~$0 at rest). EKS is
discouraged: a ~$73/mo control plane just to exist, for an occasional summary
job. That choice decides the shape of this directory — a plain handler for
Lambda, or FastAPI + a poll loop with `/healthz` for Fargate/EKS.

What is already settled either way: the `worker` IAM policy attaches to
whichever compute wins; only the trust relationship differs. Infrastructure is
Pulumi (`infra/`) — there is no Terraform in this repo, and if the EKS branch
is ever taken, the cluster would be Pulumi's while Helm owns the workloads.
