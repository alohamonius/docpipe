"""Async summary path: SQS main queue + dead-letter queue with redrive.

After ``max_receive_count`` failed receives, SQS moves a poison message to the
DLQ so one bad job can't block the queue forever. The DLQ is locked so only the
main queue can redrive into it.
"""

from __future__ import annotations

import json

import pulumi_aws as aws

import pulumi


class Messaging(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        *,
        visibility_timeout_seconds: int = 300,  # >= worker max processing time (LLM = slow)
        retention_seconds: int = 345_600,  # 4 days
        dlq_retention_seconds: int = 1_209_600,  # 14 days (SQS max)
        max_receive_count: int = 5,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:messaging:Messaging", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        self.dlq = aws.sqs.Queue(
            f"{prefix}-summaries-dlq",
            name=f"{prefix}-summaries-dlq",
            message_retention_seconds=dlq_retention_seconds,
            sqs_managed_sse_enabled=True,
            tags={"Name": f"{prefix}-summaries-dlq"},
            opts=me,
        )

        self.main = aws.sqs.Queue(
            f"{prefix}-summaries",
            name=f"{prefix}-summaries",
            visibility_timeout_seconds=visibility_timeout_seconds,
            message_retention_seconds=retention_seconds,
            sqs_managed_sse_enabled=True,
            redrive_policy=self.dlq.arn.apply(
                lambda arn: json.dumps(
                    {"deadLetterTargetArn": arn, "maxReceiveCount": max_receive_count}
                )
            ),
            tags={"Name": f"{prefix}-summaries"},
            opts=me,
        )
        self.queue_url = self.main.url
        self.queue_arn = self.main.arn
        self.dlq_url = self.dlq.url
        self.dlq_arn = self.dlq.arn

        aws.sqs.RedriveAllowPolicy(
            f"{prefix}-dlq-redrive-allow",
            queue_url=self.dlq.id,
            redrive_allow_policy=self.main.arn.apply(
                lambda arn: json.dumps({"redrivePermission": "byQueue", "sourceQueueArns": [arn]})
            ),
            opts=me,
        )

        self.register_outputs({"queue_url": self.queue_url})
