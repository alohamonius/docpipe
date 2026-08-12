"""Least-privilege IAM: the API Lambda role + a standalone worker policy.

The API Lambda gets a full role (trust is simple). The async worker gets a
permission POLICY only, so it can attach to whatever runs it (Lambda/Fargate/EKS)
— the trust/role is created next to the chosen compute in Phase 5.
"""

from __future__ import annotations

import json

import pulumi_aws as aws

import pulumi


class Iam(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        account_id: str,
        region: str,
        bedrock_model_id: str,
        *,
        documents_bucket_arn: pulumi.Input[str],
        jobs_table_arn: pulumi.Input[str],
        conversations_table_arn: pulumi.Input[str],
        queue_arn: pulumi.Input[str],
        knowledge_base_arn: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:iam:Iam", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        # Invoking a cross-region inference profile needs permission on BOTH the
        # profile ARN and the underlying foundation-model ARNs (region wildcard).
        invoke_resources = [
            f"arn:aws:bedrock:{region}:{account_id}:inference-profile/{bedrock_model_id}",
            "arn:aws:bedrock:*::foundation-model/deepseek*",
        ]

        self.lambda_role = aws.iam.Role(
            f"{prefix}-api-lambda",
            name=f"{prefix}-api-lambda",
            assume_role_policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "lambda.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
            tags={"Name": f"{prefix}-api-lambda"},
            opts=me,
        )
        self.lambda_role_arn = self.lambda_role.arn

        aws.iam.RolePolicyAttachment(
            f"{prefix}-lambda-logs",
            role=self.lambda_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            opts=me,
        )

        aws.iam.RolePolicy(
            f"{prefix}-api-lambda-policy",
            name=f"{prefix}-api-lambda-policy",
            role=self.lambda_role.id,
            policy=pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "WriteDocuments",
                            "Effect": "Allow",
                            "Action": ["s3:PutObject", "s3:GetObject"],
                            "Resource": pulumi.Output.concat(documents_bucket_arn, "/*"),
                        },
                        {
                            "Sid": "PersistUserState",
                            "Effect": "Allow",
                            "Action": [
                                "dynamodb:PutItem",
                                "dynamodb:GetItem",
                                "dynamodb:UpdateItem",
                                "dynamodb:Query",
                                "dynamodb:DeleteItem",
                            ],
                            "Resource": [
                                jobs_table_arn,
                                pulumi.Output.concat(jobs_table_arn, "/index/*"),
                                conversations_table_arn,
                                pulumi.Output.concat(conversations_table_arn, "/index/*"),
                            ],
                        },
                        {
                            "Sid": "EnqueueSummaryJobs",
                            "Effect": "Allow",
                            "Action": "sqs:SendMessage",
                            "Resource": queue_arn,
                        },
                        {
                            # The app retrieves; Bedrock uses the KB's OWN role to
                            # embed the query, so the app needs Retrieve, not Titan.
                            "Sid": "RetrieveFromKnowledgeBase",
                            "Effect": "Allow",
                            "Action": ["bedrock:Retrieve"],
                            "Resource": knowledge_base_arn,
                        },
                        {
                            "Sid": "InvokeChatModel",
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                            ],
                            "Resource": invoke_resources,
                        },
                    ],
                }
            ),
            opts=me,
        )

        self.worker_policy = aws.iam.Policy(
            f"{prefix}-worker-policy",
            name=f"{prefix}-worker-policy",
            description=(
                "Permissions for the async summary worker (SQS consumer); attach to "
                "its runtime role (Lambda/Fargate/EKS) in Phase 5"
            ),
            policy=pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "ConsumeQueue",
                            "Effect": "Allow",
                            "Action": [
                                "sqs:ReceiveMessage",
                                "sqs:DeleteMessage",
                                "sqs:GetQueueAttributes",
                            ],
                            "Resource": queue_arn,
                        },
                        {
                            "Sid": "UpdateJobs",
                            "Effect": "Allow",
                            "Action": [
                                "dynamodb:PutItem",
                                "dynamodb:UpdateItem",
                                "dynamodb:GetItem",
                            ],
                            "Resource": jobs_table_arn,
                        },
                        {
                            "Sid": "ReadDocuments",
                            "Effect": "Allow",
                            "Action": "s3:GetObject",
                            "Resource": pulumi.Output.concat(documents_bucket_arn, "/*"),
                        },
                        {
                            "Sid": "InvokeSummaryModel",
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                            ],
                            "Resource": invoke_resources,
                        },
                    ],
                }
            ),
            tags={"Name": f"{prefix}-worker-policy"},
            opts=me,
        )
        self.worker_policy_arn = self.worker_policy.arn

        self.register_outputs({})
