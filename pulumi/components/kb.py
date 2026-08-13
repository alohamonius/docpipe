"""Knowledge Base grounding: S3 Vectors store + Bedrock KB (Titan embeddings).

Pipeline: corpus markdown → KB source S3 bucket → Bedrock embeds each file with
Titan v2 → vectors land in the S3 Vectors index that retrieval queries.

Chunking is deliberately **disabled** (`NONE`) — the corpus is already chunk-final
when it arrives. See the comment on the data source below.

The embedding dimension is pinned to the SAME value in both the S3 Vectors index
AND the KB's embedding config — Titan v2 can emit 256/512/1024 and mixing them
corrupts the index.

S3 Vectors isn't in pulumi-aws (7.40), so its bucket+index come from a small
boto3-backed dynamic provider (components/s3vectors.py); the Bedrock KB + data
source are native pulumi-aws (aws.bedrock.Agent*, arg names verified). Worth a
`pulumi preview` sanity pass on the KB nested config on first run.
"""

from __future__ import annotations

import json

import pulumi_aws as aws

import pulumi
from components.s3vectors import S3VectorsIndex


class KnowledgeBase(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        account_id: str,
        region: str,
        *,
        embedding_model_id: str = "amazon.titan-embed-text-v2:0",
        embedding_dimension: int = 1024,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:kb:KnowledgeBase", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        embedding_model_arn = f"arn:aws:bedrock:{region}::foundation-model/{embedding_model_id}"

        # Source bucket the corpus syncs into.
        source = aws.s3.Bucket(
            f"{prefix}-kb-source",
            bucket=f"{prefix}-kb-source-{account_id}",
            tags={"Name": f"{prefix}-kb-source"},
            opts=me,
        )
        self.source_bucket = source.id
        aws.s3.BucketPublicAccessBlock(
            f"{prefix}-kb-source-pab",
            bucket=source.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=me,
        )
        aws.s3.BucketVersioning(
            f"{prefix}-kb-source-versioning",
            bucket=source.id,
            versioning_configuration={"status": "Enabled"},
            opts=me,
        )
        aws.s3.BucketServerSideEncryptionConfiguration(
            f"{prefix}-kb-source-sse",
            bucket=source.id,
            rules=[{"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}],
            opts=me,
        )

        # S3 Vectors store (boto3-backed dynamic provider — pulumi-aws lacks it).
        # Dimension MUST match the embedding model output (Titan v2 = 1024).
        store = S3VectorsIndex(
            f"{prefix}-vectors",
            region=region,
            vector_bucket_name=f"{prefix}-vectors",
            index_name=f"{prefix}-kb",
            dimension=embedding_dimension,
            distance_metric="cosine",
            data_type="float32",
            opts=me,
        )

        # KB service role — Bedrock assumes it to embed + read/write vectors.
        role = aws.iam.Role(
            f"{prefix}-kb-role",
            name=f"{prefix}-kb-role",
            assume_role_policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"Service": "bedrock.amazonaws.com"},
                            "Action": "sts:AssumeRole",
                            # Confused-deputy guard.
                            "Condition": {"StringEquals": {"aws:SourceAccount": account_id}},
                        }
                    ],
                }
            ),
            tags={"Name": f"{prefix}-kb-role"},
            opts=me,
        )
        role_policy = aws.iam.RolePolicy(
            f"{prefix}-kb-policy",
            name=f"{prefix}-kb-policy",
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
                            "Resource": [source.arn, pulumi.Output.concat(source.arn, "/*")],
                        },
                        {
                            "Sid": "AccessVectorIndex",
                            "Effect": "Allow",
                            "Action": "s3vectors:*",
                            "Resource": [store.vector_bucket_arn, store.index_arn],
                        },
                    ],
                }
            ),
            opts=me,
        )

        kb = aws.bedrock.AgentKnowledgeBase(
            f"{prefix}-kb",
            name=f"{prefix}-kb",
            role_arn=role.arn,
            knowledge_base_configuration={
                "type": "VECTOR",
                "vector_knowledge_base_configuration": {
                    "embedding_model_arn": embedding_model_arn,
                    # Pin the output dimension so it can never drift from the index.
                    "embedding_model_configuration": {
                        "bedrock_embedding_model_configuration": {
                            "dimensions": embedding_dimension,
                            "embedding_data_type": "FLOAT32",
                        }
                    },
                },
            },
            storage_configuration={
                "type": "S3_VECTORS",
                "s3_vectors_configuration": {"index_arn": store.index_arn},
            },
            opts=pulumi.ResourceOptions(parent=self, depends_on=[role_policy]),
        )
        self.knowledge_base_id = kb.id
        self.knowledge_base_arn = kb.arn

        ds = aws.bedrock.AgentDataSource(
            f"{prefix}-kb-source-ds",
            knowledge_base_id=kb.id,
            name=f"{prefix}-kb-source",
            data_source_configuration={
                "type": "S3",
                "s3_configuration": {"bucket_arn": source.arn},
            },
            # NONE = one chunk per file. NOT a default we fell into — the corpus
            # arrives pre-chunked from health.studio's `pnpm kb:build`, where each
            # file is already one retrievable unit carrying its own evidence legend
            # (the ★ scale + "not a diagnosis" disclaimer, in a header block).
            # Any splitter — FIXED_SIZE, SEMANTIC, HIERARCHICAL — cuts that header
            # off every fragment after the first, and the model then sees bare star
            # ratings with no scale attached. See build-kb.ts and the MANIFEST's
            # `ingestion.note` in the health.studio repo.
            vector_ingestion_configuration={
                "chunking_configuration": {"chunking_strategy": "NONE"}
            },
            opts=me,
        )
        self.data_source_id = ds.data_source_id

        self.register_outputs({})
