# services/api

Lambda handlers behind API Gateway (HTTP API). Built in Phase 3.

- `POST /summarize` — store document in S3, create job in DynamoDB, enqueue SQS
- `GET /jobs/{id}` — read job status/result from DynamoDB

Uses `docpipe-core` for all AWS access. Packaged as a zip in CI.
