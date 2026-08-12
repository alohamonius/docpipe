"""Stateful layer: documents S3 bucket, per-user DynamoDB tables, optional Aurora.

Aurora is off by default; when on, ``aurora_min_acu=0`` makes Serverless v2
AUTO-PAUSE when idle (idle cost ≈ $0). It's only for the Phase 5b pgvector
benchmark. DynamoDB is per-user single-table style (PK=userId) with on-demand
billing + TTL garbage collection.
"""

from __future__ import annotations

import pulumi_aws as aws

import pulumi


class Data(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        account_id: str,
        *,
        enable_aurora: bool = False,
        vpc_id: pulumi.Input[str] | None = None,
        vpc_cidr: str | None = None,
        private_subnet_ids: list[pulumi.Input[str]] | None = None,
        document_retention_days: int = 30,
        aurora_min_acu: float = 0,
        aurora_max_acu: float = 1,
        aurora_engine_version: str = "16.6",
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:data:Data", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        # S3 bucket names are globally unique → suffix with the account id.
        docs = aws.s3.Bucket(
            f"{prefix}-documents",
            bucket=f"{prefix}-documents-{account_id}",
            tags={"Name": f"{prefix}-documents"},
            opts=me,
        )
        self.documents_bucket = docs.id
        self.documents_bucket_arn = docs.arn

        aws.s3.BucketPublicAccessBlock(
            f"{prefix}-documents-pab",
            bucket=docs.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=me,
        )
        aws.s3.BucketVersioning(
            f"{prefix}-documents-versioning",
            bucket=docs.id,
            versioning_configuration={"status": "Enabled"},
            opts=me,
        )
        aws.s3.BucketServerSideEncryptionConfiguration(
            f"{prefix}-documents-sse",
            bucket=docs.id,
            rules=[{"apply_server_side_encryption_by_default": {"sse_algorithm": "AES256"}}],
            opts=me,
        )
        aws.s3.BucketLifecycleConfiguration(
            f"{prefix}-documents-lifecycle",
            bucket=docs.id,
            rules=[
                {
                    "id": "expire-raw-docs",
                    "status": "Enabled",
                    "filter": {"prefix": "documents/"},
                    "expiration": {"days": document_retention_days},
                    "noncurrent_version_expiration": {"noncurrent_days": 7},
                }
            ],
            opts=me,
        )

        self.jobs = aws.dynamodb.Table(
            f"{prefix}-jobs",
            name=f"{prefix}-jobs",
            billing_mode="PAY_PER_REQUEST",
            hash_key="userId",
            range_key="jobId",
            attributes=[{"name": "userId", "type": "S"}, {"name": "jobId", "type": "S"}],
            ttl={"attribute_name": "expiresAt", "enabled": True},
            point_in_time_recovery={"enabled": True},
            tags={"Name": f"{prefix}-jobs"},
            opts=me,
        )
        self.jobs_table = self.jobs.name
        self.jobs_table_arn = self.jobs.arn

        self.conversations = aws.dynamodb.Table(
            f"{prefix}-conversations",
            name=f"{prefix}-conversations",
            billing_mode="PAY_PER_REQUEST",
            hash_key="userId",
            range_key="conversationId",
            attributes=[
                {"name": "userId", "type": "S"},
                {"name": "conversationId", "type": "S"},
            ],
            ttl={"attribute_name": "expiresAt", "enabled": True},
            point_in_time_recovery={"enabled": True},
            tags={"Name": f"{prefix}-conversations"},
            opts=me,
        )
        self.conversations_table = self.conversations.name
        self.conversations_table_arn = self.conversations.arn

        if enable_aurora:
            subnet_group = aws.rds.SubnetGroup(
                f"{prefix}-aurora",
                name=f"{prefix}-aurora",
                subnet_ids=private_subnet_ids or [],
                tags={"Name": f"{prefix}-aurora"},
                opts=me,
            )
            sg = aws.ec2.SecurityGroup(
                f"{prefix}-aurora",
                name=f"{prefix}-aurora",
                description="Aurora — Postgres reachable only from inside the VPC",
                vpc_id=vpc_id,
                ingress=[
                    {
                        "description": "Postgres from within the VPC",
                        "from_port": 5432,
                        "to_port": 5432,
                        "protocol": "tcp",
                        "cidr_blocks": [vpc_cidr],
                    }
                ],
                egress=[
                    {"from_port": 0, "to_port": 0, "protocol": "-1", "cidr_blocks": ["0.0.0.0/0"]}
                ],
                tags={"Name": f"{prefix}-aurora"},
                opts=me,
            )
            cluster = aws.rds.Cluster(
                f"{prefix}-aurora",
                cluster_identifier=f"{prefix}-aurora",
                engine="aurora-postgresql",
                engine_version=aurora_engine_version,
                database_name="docpipe",
                master_username="docpipe",
                # AWS stores the master password in Secrets Manager — never in state.
                manage_master_user_password=True,
                db_subnet_group_name=subnet_group.name,
                vpc_security_group_ids=[sg.id],
                storage_encrypted=True,
                skip_final_snapshot=True,  # dev only
                serverlessv2_scaling_configuration={
                    "min_capacity": aurora_min_acu,
                    "max_capacity": aurora_max_acu,
                },
                opts=me,
            )
            aws.rds.ClusterInstance(
                f"{prefix}-aurora-1",
                identifier=f"{prefix}-aurora-1",
                cluster_identifier=cluster.id,
                instance_class="db.serverless",
                engine=cluster.engine,
                engine_version=cluster.engine_version,
                opts=me,
            )

        self.register_outputs({})
