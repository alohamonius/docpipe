# infra

Terraform for docpipe. Reusable modules + one live environment (`envs/dev`).

## Modules

| Module       | Owns                                                              | Phase |
|--------------|-------------------------------------------------------------------|-------|
| `network`    | VPC, subnets ×2 AZ, single NAT, S3/DynamoDB gateway endpoints     | 2     |
| `iam`        | Per-service roles + least-privilege policies                      | 2     |
| `data`       | S3 bucket, DynamoDB jobs table, Aurora Serverless v2              | 2     |
| `messaging`  | SQS queue + DLQ + redrive policy                                  | 2     |
| `api-lambda` | HTTP API Gateway, Lambda, log group, throttling                   | 3     |
| `eks`        | EKS cluster, managed node group, IRSA                             | 4     |
| `monitoring` | CloudWatch dashboard, alarms, SNS                                 | 5     |

## State & secrets

Remote state in S3 with the native lockfile (Terraform ≥ 1.10 — no DynamoDB
lock table needed). Per-env backend config and variable values are gitignored;
copy the examples and fill in your own:

```bash
cd envs/dev
cp backend.hcl.example backend.hcl        # your state bucket
cp terraform.tfvars.example terraform.tfvars
terraform init -backend-config=backend.hcl
```

Nothing in this directory contains account-specific values; everything real
flows in via `backend.hcl` and `terraform.tfvars`.
