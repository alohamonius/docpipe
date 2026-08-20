"""Stateful layer: documents S3 bucket, per-user DynamoDB tables, optional Aurora.

Aurora is off by default; when on, ``aurora_min_acu=0`` makes Serverless v2
AUTO-PAUSE when idle (idle cost ≈ $0, since ACU billing pauses — storage and
backup do not). It exists for the Phase 5b pgvector benchmark. DynamoDB is
per-user single-table style (PK=userId) with on-demand billing + TTL garbage
collection.

**Aurora here is only half of what a Bedrock KB needs, and the half Pulumi can
do.** This component gets the cluster to the point where something can connect
to it: Data API on, a dedicated ``bedrock_user`` secret allocated. It cannot
create the pgvector extension, the ``bedrock_user`` Postgres role, the
``bedrock_integration.bedrock_kb`` table or its three indexes — that is SQL, and
there is no SQL provider in this stack. ``scripts/aurora_bootstrap.py`` runs it
over the Data API. Sequence, and it is genuinely two applies:

1. ``enableAurora: true`` → ``pulumi up``  (cluster + secrets exist)
2. ``python scripts/aurora_bootstrap.py``  (extension, role, schema, indexes)
3. ``enableAuroraKb: true`` → ``pulumi up``  (the KB attaches)

Step 3 before step 2 fails: Bedrock validates the connection when it creates the
knowledge base, so it needs the table and the role to already be there.

**The bedrock_user password is deliberately not in Pulumi state.** This module
creates the *empty* Secrets Manager secret; the bootstrap script generates the
password and writes the first version. No ``SecretVersion`` is declared here, so
Pulumi never reads the credential and never diffs on it.
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
        # 16.1+ required by Bedrock KB (pgvector >= 0.5.0 ships from 16.1).
        # RDS retires patch versions: 16.6 vanished from us-east-1 and failed
        # the first apply (2026-08-20); pin whatever `describe-db-engine-versions`
        # currently lists when this errors again.
        aurora_engine_version: str = "16.14",
        aurora_database_name: str = "docpipe",
        aurora_bedrock_username: str = "bedrock_user",
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

        # Aurora outputs default to None so `__main__` can branch on them without
        # caring whether the flag was set. The previous version of this component
        # exported nothing at all, which meant the cluster it created could not be
        # referenced by anything — including the KB it exists to back.
        self.aurora_cluster_arn: pulumi.Output[str] | None = None
        self.aurora_cluster_endpoint: pulumi.Output[str] | None = None
        self.aurora_database_name: str | None = None
        self.aurora_master_secret_arn: pulumi.Output[str] | None = None
        self.aurora_bedrock_secret_arn: pulumi.Output[str] | None = None
        self.aurora_bedrock_username: str | None = None

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
                # ASCII only: EC2 rejects non-ASCII in GroupDescription (an
                # em-dash here failed the first apply, 2026-08-20).
                description="Aurora - Postgres reachable only from inside the VPC",
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
                database_name=aurora_database_name,
                master_username="docpipe",
                # AWS stores the master password in Secrets Manager — never in state.
                # This is the MASTER credential: the bootstrap script uses it, and
                # Bedrock must never see it. Bedrock gets `bedrock_user` below.
                manage_master_user_password=True,
                # Bedrock KB reaches Aurora over the RDS Data API, not a socket —
                # so this is not optional, it is the entire connection path. The
                # cluster is in private subnets with no NAT; the Data API is a
                # regional endpoint Bedrock calls with IAM, which is exactly why
                # this design needs no NAT gateway.
                enable_http_endpoint=True,
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

            # Bedrock's own least-privilege credential, separate from the master.
            # Created EMPTY on purpose — see the module docstring. The bootstrap
            # script generates the password, writes version 1, and creates the
            # matching Postgres role in the same run, so the secret and the role
            # can never disagree.
            bedrock_secret = aws.secretsmanager.Secret(
                f"{prefix}-aurora-bedrock-user",
                name=f"{prefix}-aurora-bedrock-user",
                description=(
                    "Bedrock Knowledge Base credential for Aurora pgvector. "
                    "Populated by scripts/aurora_bootstrap.py, not by Pulumi."
                ),
                # Dev: allow a fast re-create rather than the 7-day default window.
                recovery_window_in_days=0,
                tags={"Name": f"{prefix}-aurora-bedrock-user"},
                opts=me,
            )

            self.aurora_cluster_arn = cluster.arn
            self.aurora_cluster_endpoint = cluster.endpoint
            self.aurora_database_name = aurora_database_name
            # master_user_secrets is a list output; index 0 is the managed secret.
            self.aurora_master_secret_arn = cluster.master_user_secrets.apply(
                lambda s: s[0].secret_arn if s else ""
            )
            self.aurora_bedrock_secret_arn = bedrock_secret.arn
            self.aurora_bedrock_username = aurora_bedrock_username

        self.register_outputs({})
