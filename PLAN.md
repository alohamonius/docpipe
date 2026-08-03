# Build plan

Phased so every phase leaves the repo in a working, demonstrable state.

## Phase 0 — Init & setup ✅

- [x] Repo skeleton: packages / services / infra layout
- [x] uv workspace, `docpipe-core` package stub, pytest wired
- [x] Terraform skeleton: modules, dev env, versions, backend example
- [x] .gitignore covering tfstate / tfvars / .env / caches
- [x] Makefile with fmt / lint / test / infra targets

## Phase 1 — Shared package (`docpipe-core`)

The heart of the demo: one library reused by Lambda and the EKS worker.

- [ ] `models.py` — Pydantic: `Job`, `JobStatus`, `SummaryRequest`, `SummaryResult`
- [ ] `storage.py` — S3 (put/get document) + DynamoDB (job CRUD) wrappers
- [ ] `queue.py` — SQS publish/consume with visibility-timeout handling
- [ ] `llm.py` — Bedrock client: invoke Claude, retries/backoff, token usage capture
- [ ] `observability.py` — structured JSON logging + CloudWatch EMF metrics
- [ ] Unit tests with moto (S3/DynamoDB/SQS mocked); Bedrock behind an interface
- [ ] CI: GitHub Actions — ruff, pytest, mypy

## Phase 2 — Terraform foundation

- [ ] `network` module: VPC, public/private subnets ×2 AZ, single NAT, endpoints
      (S3 + DynamoDB gateway endpoints — free, keeps traffic off the NAT)
- [ ] `iam` module: per-service roles, least-privilege policies (api-lambda:
      S3 put + DDB write + SQS send; worker: SQS consume + Bedrock invoke + DDB/RDS)
- [ ] `data` module: S3 bucket (versioned, SSE), DynamoDB table (PK jobId, TTL),
      Aurora Serverless v2 (min ACU, private subnets)
- [ ] `messaging` module: SQS queue + DLQ + redrive policy
- [ ] Remote state: S3 backend with native lockfile; `backend.hcl` per env
- [ ] `make infra-up` / `make infra-down` for the dev env

## Phase 3 — API service (Lambda + API Gateway)

- [ ] `services/api`: handlers for `POST /summarize`, `GET /jobs/{id}` using core
- [ ] Packaging: zip build via uv export → CI artifact
- [ ] `api-lambda` module: HTTP API Gateway, routes, Lambda, log group, throttling
- [ ] Smoke test script hitting the deployed endpoint

## Phase 4 — Worker (EKS + Bedrock)

- [ ] `services/worker`: FastAPI app + SQS poll loop, Dockerfile
- [ ] `eks` module: cluster, one small managed node group, IRSA for the worker role
- [ ] Helm chart (or plain manifests) — deliberately *not* Terraform-managed
- [ ] End-to-end: POST doc → summary lands in DynamoDB + Aurora

## Phase 5 — Observability & polish

- [ ] `monitoring` module: CloudWatch dashboard, alarms (DLQ depth > 0,
      Lambda errors, worker restarts), SNS email
- [ ] README: architecture diagram render, demo GIF/recording, cost table
- [ ] `terraform destroy` clean-pass verified (no orphaned resources)

## Non-goals

- Multi-env promotion (only `dev`), custom domains, auth beyond API keys,
  multi-region. Noted in README as conscious scope cuts.
