"""Platform-level safety: a Bedrock guardrail + an audit trail for every call.

The guardrail enforces the non-diagnostic stance even against prompt injection
(the system prompt alone can be talked around); invocation logging records what
the model was asked and answered.

The guardrail is native pulumi-aws (nested arg names verified: topics_configs /
filters_configs / pii_entities_configs). Invocation logging has no pulumi-aws
resource, so it's set via a boto3-backed dynamic provider
(components/invocation_logging.py).
"""

from __future__ import annotations

import json

import pulumi_aws as aws

import pulumi
from components.invocation_logging import InvocationLogging

_DEFAULT_BLOCKED = (
    "I can't help with that. I'm a non-diagnostic assistant — for a diagnosis, "
    "prescriptions, or urgent symptoms, please consult a licensed clinician."
)


class Safety(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        account_id: str,
        region: str,
        *,
        blocked_message: str = _DEFAULT_BLOCKED,
        grounding_threshold: float = 0.75,
        relevance_threshold: float = 0.75,
        log_retention_days: int = 30,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:safety:Safety", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        self.guardrail = aws.bedrock.Guardrail(
            f"{prefix}-guardrail",
            name=f"{prefix}-guardrail",
            description="Non-diagnostic health assistant: no diagnosis, PII-safe, grounded",
            blocked_input_messaging=blocked_message,
            blocked_outputs_messaging=blocked_message,
            topic_policy_config={
                "topics_configs": [
                    {
                        "name": "MedicalDiagnosis",
                        "type": "DENY",
                        "definition": (
                            "Identifying, confirming, or ruling out a disease, condition, or "
                            "injury for an individual based on their symptoms."
                        ),
                        "examples": [
                            "What disease do I have?",
                            "Is this lump cancer?",
                            "Do I have a torn ACL?",
                        ],
                    },
                    {
                        "name": "MedicalPrescription",
                        "type": "DENY",
                        "definition": (
                            "Prescribing or giving dosages for medications, drugs, or specific "
                            "medical treatments for an individual."
                        ),
                        "examples": [
                            "How much ibuprofen should I take for this?",
                            "What medication should I use for my back pain?",
                        ],
                    },
                ]
            },
            content_policy_config={
                # PROMPT_ATTACK is input-only — AWS requires output_strength=NONE.
                "filters_configs": [
                    {"type": "PROMPT_ATTACK", "input_strength": "HIGH", "output_strength": "NONE"},
                    {"type": "HATE", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                    {"type": "VIOLENCE", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                    {"type": "SEXUAL", "input_strength": "HIGH", "output_strength": "HIGH"},
                    {"type": "INSULTS", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                    {"type": "MISCONDUCT", "input_strength": "MEDIUM", "output_strength": "MEDIUM"},
                ]
            },
            sensitive_information_policy_config={
                "pii_entities_configs": [
                    {"type": t, "action": "ANONYMIZE"}
                    for t in ["EMAIL", "PHONE", "NAME", "ADDRESS", "US_SOCIAL_SECURITY_NUMBER"]
                ]
            },
            contextual_grounding_policy_config={
                "filters_configs": [
                    {"type": "GROUNDING", "threshold": grounding_threshold},
                    {"type": "RELEVANCE", "threshold": relevance_threshold},
                ]
            },
            opts=me,
        )
        self.guardrail_id = self.guardrail.guardrail_id
        self.guardrail_arn = self.guardrail.guardrail_arn
        self.guardrail_version = "DRAFT"

        # ---- Model invocation logging → S3 + CloudWatch --------------------
        logs = aws.s3.Bucket(
            f"{prefix}-bedrock-logs",
            bucket=f"{prefix}-bedrock-logs-{account_id}",
            tags={"Name": f"{prefix}-bedrock-logs"},
            opts=me,
        )
        self.log_bucket = logs.id
        aws.s3.BucketPublicAccessBlock(
            f"{prefix}-bedrock-logs-pab",
            bucket=logs.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=me,
        )
        aws.s3.BucketServerSideEncryptionConfiguration(
            f"{prefix}-bedrock-logs-sse",
            bucket=logs.id,
            rules=[{"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}],
            opts=me,
        )
        bucket_policy = aws.s3.BucketPolicy(
            f"{prefix}-bedrock-logs-policy",
            bucket=logs.id,
            policy=pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AllowBedrockLogDelivery",
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock.amazonaws.com"},
                            "Action": "s3:PutObject",
                            "Resource": pulumi.Output.concat(logs.arn, "/*"),
                            "Condition": {
                                "StringEquals": {"aws:SourceAccount": account_id},
                                "ArnLike": {
                                    "aws:SourceArn": f"arn:aws:bedrock:{region}:{account_id}:*"
                                },
                            },
                        }
                    ],
                }
            ),
            opts=me,
        )

        log_group = aws.cloudwatch.LogGroup(
            f"{prefix}-bedrock-invocations",
            name=f"/aws/bedrock/{prefix}/invocations",
            retention_in_days=log_retention_days,
            tags={"Name": f"{prefix}-bedrock-invocations"},
            opts=me,
        )

        logging_role = aws.iam.Role(
            f"{prefix}-bedrock-logging",
            name=f"{prefix}-bedrock-logging",
            assume_role_policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
                        }
                    ],
                }
            ),
            tags={"Name": f"{prefix}-bedrock-logging"},
            opts=me,
        )
        logging_role_policy = aws.iam.RolePolicy(
            f"{prefix}-bedrock-logging-policy",
            name=f"{prefix}-bedrock-logging",
            role=logging_role.id,
            policy=pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                            "Resource": pulumi.Output.concat(log_group.arn, ":*"),
                        }
                    ],
                }
            ),
            opts=me,
        )

        InvocationLogging(
            f"{prefix}-invocation-logging",
            region=region,
            bucket_name=logs.id,
            log_group_name=log_group.name,
            role_arn=logging_role.arn,
            # Bucket policy + log-group role must exist before Bedrock validates it.
            opts=pulumi.ResourceOptions(
                parent=self, depends_on=[bucket_policy, logging_role_policy]
            ),
        )

        self.register_outputs({})
