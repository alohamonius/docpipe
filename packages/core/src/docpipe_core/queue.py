"""SQS publish/consume for the jobs queue."""

from __future__ import annotations

import json
from typing import Any

import boto3
from pydantic import BaseModel


class QueueMessage(BaseModel):
    job_id: str
    receipt_handle: str


class JobQueue:
    def __init__(self, queue_url: str, sqs_client: Any = None) -> None:
        self.queue_url = queue_url
        self._sqs = sqs_client or boto3.client("sqs")

    def send(self, job_id: str) -> str:
        response = self._sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps({"job_id": job_id}),
        )
        return response["MessageId"]  # type: ignore[no-any-return]

    def receive(self, max_messages: int = 1, wait_seconds: int = 20) -> list[QueueMessage]:
        response = self._sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
        )
        messages = []
        for raw in response.get("Messages", []):
            body = json.loads(raw["Body"])
            messages.append(
                QueueMessage(job_id=body["job_id"], receipt_handle=raw["ReceiptHandle"])
            )
        return messages

    def delete(self, receipt_handle: str) -> None:
        self._sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt_handle)
