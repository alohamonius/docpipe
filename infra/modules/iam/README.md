# module: iam (Phase 2)

Per-service roles, least privilege:

- `api-lambda` role: S3 PutObject (docs prefix), DynamoDB PutItem/GetItem
  (jobs table), SQS SendMessage (jobs queue).
- `worker` role (assumed via IRSA from EKS): SQS ReceiveMessage/DeleteMessage,
  Bedrock InvokeModel (specific model ARN), DynamoDB UpdateItem, RDS connect.

No wildcard resources; every policy names its ARN.
