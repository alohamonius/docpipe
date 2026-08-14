"""The second Knowledge Base: same corpus, same embeddings, Aurora pgvector.

This is the other half of the Phase 5b benchmark. It is deliberately a separate
component from ``kb.py`` rather than a flag on it, because the *point* is that
the two KBs differ in exactly one dimension — the storage slot — and reading two
files side by side proves that better than reading one file with a branch in it.

Everything else is held identical on purpose, and the benchmark is worthless if
it drifts:

- the same source S3 bucket (passed in, not created here), so both KBs index
  byte-identical objects under the same keys
- the same ``amazon.titan-embed-text-v2:0`` at the same 1024 dims / FLOAT32
- the same ``chunkingStrategy: NONE``, for the same reason as the S3 Vectors
  data source: the corpus is chunk-final and every chunk carries its own
  evidence legend in a header a splitter would cut off

**Requires ``scripts/aurora_bootstrap.py`` to have run.** Bedrock validates the
database connection at ``CreateKnowledgeBase`` time — it connects, and it expects
``bedrock_integration.bedrock_kb`` and the ``bedrock_user`` role to exist. If they
do not, the create fails rather than degrading, so this component is gated behind
its own ``enableAuroraKb`` flag separate from ``enableAurora``.

The field mapping is not free-form: Bedrock writes these exact columns, and they
must match the DDL the bootstrap script applies.
"""

from __future__ import annotations

import json

import pulumi_aws as aws

import pulumi


class AuroraKnowledgeBase(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        account_id: str,
        region: str,
        *,
        source_bucket_arn: pulumi.Input[str],
        cluster_arn: pulumi.Input[str],
        credentials_secret_arn: pulumi.Input[str],
        database_name: str,
        embedding_model_id: str = "amazon.titan-embed-text-v2:0",
        embedding_dimension: int = 1024,
        table_name: str = "bedrock_integration.bedrock_kb",
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:kb:AuroraKnowledgeBase", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        embedding_model_arn = f"arn:aws:bedrock:{region}::foundation-model/{embedding_model_id}"

        role = aws.iam.Role(
            f"{prefix}-kb-aurora-role",
            name=f"{prefix}-kb-aurora-role",
            assume_role_policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                            # Confused-deputy guard, same as the S3 Vectors KB role.
                            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
                        }
                    ],
                }
            ),
            tags={"Name": f"{prefix}-kb-aurora-role"},
            opts=me,
        )
        role_policy = aws.iam.RolePolicy(
            f"{prefix}-kb-aurora-policy",
            name=f"{prefix}-kb-aurora-policy",
            role=role.id,
            policy=pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "InvokeEmbeddingModel",
                            "Effect": "Allow",
                            "Action": "bedrock:InvokeModel",
                            "Resource": embedding_model_arn,
                        },
                        {
                            "Sid": "ReadSourceBucket",
                            "Effect": "Allow",
                            "Action": ["s3:GetObject", "s3:ListBucket"],
                            "Resource": [
                                source_bucket_arn,
                                pulumi.Output.concat(source_bucket_arn, "/*"),
                            ],
                        },
                        {
                            # This is the connection path. No socket, no VPC
                            # attachment, no NAT — Bedrock calls the Data API.
                            "Sid": "QueryClusterOverDataApi",
                            "Effect": "Allow",
                            "Action": [
                                "rds-data:ExecuteStatement",
                                "rds-data:BatchExecuteStatement",
                                "rds-data:BeginTransaction",
                                "rds-data:CommitTransaction",
                                "rds-data:RollbackTransaction",
                            ],
                            "Resource": cluster_arn,
                        },
                        {
                            # Scoped to bedrock_user's secret only — the master
                            # secret is a different ARN and stays out of reach.
                            "Sid": "ReadBedrockUserCredential",
                            "Effect": "Allow",
                            "Action": "secretsmanager:GetSecretValue",
                            "Resource": credentials_secret_arn,
                        },
                    ],
                }
            ),
            opts=me,
        )

        kb = aws.bedrock.AgentKnowledgeBase(
            f"{prefix}-kb-aurora",
            name=f"{prefix}-kb-aurora",
            role_arn=role.arn,
            knowledge_base_configuration={
                "type": "VECTOR",
                "vector_knowledge_base_configuration": {
                    "embedding_model_arn": embedding_model_arn,
                    # Identical to the S3 Vectors KB. If these two ever diverge,
                    # the benchmark measures the embedding change, not the store.
                    "embedding_model_configuration": {
                        "bedrock_embedding_model_configuration": {
                            "dimensions": embedding_dimension,
                            "embedding_data_type": "FLOAT32",
                        }
                    },
                },
            },
            storage_configuration={
                "type": "RDS",
                "rds_configuration": {
                    "resource_arn": cluster_arn,
                    "credentials_secret_arn": credentials_secret_arn,
                    "database_name": database_name,
                    "table_name": table_name,
                    # These column names are Bedrock's contract, not our choice.
                    # They must match scripts/aurora_bootstrap.py's DDL exactly.
                    "field_mapping": {
                        "primary_key_field": "id",
                        "vector_field": "embedding",
                        "text_field": "chunks",
                        "metadata_field": "metadata",
                        # The sidecar attributes (maxEvidence, citationCount…)
                        # land here, GIN-indexed — which is what makes evidence
                        # filtering and hybrid search possible on this store.
                        "custom_metadata_field": "custom_metadata",
                    },
                },
            },
            opts=pulumi.ResourceOptions(parent=self, depends_on=[role_policy]),
        )
        self.knowledge_base_id = kb.id
        self.knowledge_base_arn = kb.arn

        ds = aws.bedrock.AgentDataSource(
            f"{prefix}-kb-aurora-ds",
            knowledge_base_id=kb.id,
            name=f"{prefix}-kb-aurora-source",
            data_source_configuration={
                "type": "S3",
                "s3_configuration": {"bucket_arn": source_bucket_arn},
            },
            # NONE, for the same reason as the S3 Vectors data source — and it
            # must stay NONE here too, or the two KBs stop being comparable.
            # test_kb_chunking_contract.py asserts this file as well.
            vector_ingestion_configuration={
                "chunking_configuration": {"chunking_strategy": "NONE"}
            },
            opts=me,
        )
        self.data_source_id = ds.data_source_id

        self.register_outputs({})
