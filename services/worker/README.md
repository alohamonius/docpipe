# services/worker

Containerized SQS consumer deployed to EKS. Built in Phase 4.

Polls the jobs queue, calls Bedrock (Claude) via `docpipe-core`, writes the
summary to DynamoDB and job history to Aurora. FastAPI serves `/healthz` for
k8s probes. AWS permissions come from IRSA (no static credentials in-cluster).

Kubernetes manifests live here (Helm chart) — deliberately not managed by
Terraform; Terraform owns the cluster, Helm owns the workloads.
