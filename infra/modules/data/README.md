# module: data (Phase 2)

- S3 bucket: versioned, SSE-S3, public access blocked, lifecycle to expire raw docs
- DynamoDB: `jobs` table, PK `jobId`, TTL attribute, on-demand billing
- Aurora Serverless v2 (PostgreSQL): min ACU 0.5, private subnets only
