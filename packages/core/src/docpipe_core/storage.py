"""S3 document storage and DynamoDB job records."""

from __future__ import annotations

from typing import Any

import boto3

from docpipe_core.models import Job, JobStatus, SummaryResult, utc_now


class DocumentStore:
    """Raw documents in S3, keyed by job id under a fixed prefix."""

    PREFIX = "documents/"

    def __init__(self, bucket: str, s3_client: Any = None) -> None:
        self.bucket = bucket
        self._s3 = s3_client or boto3.client("s3")

    def key_for(self, job_id: str) -> str:
        return f"{self.PREFIX}{job_id}"

    def put_document(self, job_id: str, content: bytes, content_type: str) -> str:
        key = self.key_for(job_id)
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=content, ContentType=content_type)
        return key

    def get_document(self, key: str) -> bytes:
        response = self._s3.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()  # type: ignore[no-any-return]


class JobStore:
    """Job records in DynamoDB. PK: jobId."""

    def __init__(self, table_name: str, dynamodb_resource: Any = None) -> None:
        resource = dynamodb_resource or boto3.resource("dynamodb")
        self._table = resource.Table(table_name)

    def save(self, job: Job) -> None:
        item = job.model_dump(mode="json")
        item["jobId"] = item.pop("job_id")
        self._table.put_item(Item=item)

    def get(self, job_id: str) -> Job | None:
        response = self._table.get_item(Key={"jobId": job_id})
        item = response.get("Item")
        if item is None:
            return None
        item["job_id"] = item.pop("jobId")
        return Job.model_validate(item)

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        result: SummaryResult | None = None,
        error: str | None = None,
    ) -> None:
        # "status" and "error" are DynamoDB reserved words — alias them.
        expression = "SET #status = :status, updated_at = :updated_at"
        names = {"#status": "status"}
        values: dict[str, Any] = {
            ":status": status.value,
            ":updated_at": utc_now().isoformat(),
        }
        if result is not None:
            expression += ", #result = :result"
            names["#result"] = "result"
            values[":result"] = result.model_dump(mode="json")
        if error is not None:
            expression += ", #error = :error"
            names["#error"] = "error"
            values[":error"] = error
        self._table.update_item(
            Key={"jobId": job_id},
            UpdateExpression=expression,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression="attribute_exists(jobId)",
        )
