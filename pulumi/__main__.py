"""docpipe infrastructure — dev stack entrypoint.

Wires the components and exports the values that go into .env (the same set the
old Terraform stack produced — see /.env.example). Run `pulumi stack output` to
read them after `pulumi up`.
"""

import pulumi_aws as aws
from components import AuroraKnowledgeBase, Data, Iam, KnowledgeBase, Messaging, Network, Safety

import pulumi

cfg = pulumi.Config()
prefix = cfg.get("namePrefix") or "docpipe-dev"
# Two flags, not one, and the order between them is not negotiable: Bedrock
# validates the DB connection when it creates the KB, so the cluster must exist
# AND scripts/aurora_bootstrap.py must have run before enableAuroraKb flips.
# See components/data.py's docstring for the three-step sequence.
enable_aurora = cfg.get_bool("enableAurora") or False
enable_aurora_kb = cfg.get_bool("enableAuroraKb") or False
# Two models, both DeepSeek (locked 2026-08-14, see PLAN.md). The summary model
# is INFERENCE_PROFILE-only; the chat model is ON_DEMAND. They were one config
# value, which meant the chat statement carried the summariser's profile ARN and
# worked only because a `deepseek*` wildcard happened to cover it.
bedrock_model_id = cfg.get("bedrockModelId") or "us.deepseek.r1-v1:0"
chat_model_id = cfg.get("chatModelId") or "deepseek.v3.2"

# get_* invokes resolve synchronously to plain values (not Outputs).
ident = aws.get_caller_identity()
region = aws.get_region().region
azs = aws.get_availability_zones(state="available").names[:2]

network = Network(prefix, region, azs)
data = Data(
    prefix,
    ident.account_id,
    enable_aurora=enable_aurora,
    vpc_id=network.vpc.id,
    vpc_cidr=network.vpc_cidr,
    private_subnet_ids=network.private_subnet_ids,
)
messaging = Messaging(prefix)
kb = KnowledgeBase(prefix, ident.account_id, region)
# Safety before Iam: the roles need the guardrail ARN to grant ApplyGuardrail.
safety = Safety(prefix, ident.account_id, region)
iam = Iam(
    prefix,
    ident.account_id,
    region,
    bedrock_model_id,
    chat_model_id,
    documents_bucket_arn=data.documents_bucket_arn,
    jobs_table_arn=data.jobs_table_arn,
    conversations_table_arn=data.conversations_table_arn,
    queue_arn=messaging.queue_arn,
    knowledge_base_arn=kb.knowledge_base_arn,
    guardrail_arn=safety.guardrail_arn,
)

aurora_kb: AuroraKnowledgeBase | None = None
if enable_aurora_kb:
    if not enable_aurora:
        raise ValueError(
            "enableAuroraKb requires enableAurora — there is no cluster to attach to. "
            "See components/data.py for the three-step sequence."
        )
    # Same source bucket as the S3 Vectors KB: the benchmark's whole premise is
    # that the two stores index identical objects.
    aurora_kb = AuroraKnowledgeBase(
        prefix,
        ident.account_id,
        region,
        source_bucket_arn=kb.source_bucket_arn,
        cluster_arn=data.aurora_cluster_arn,
        credentials_secret_arn=data.aurora_bedrock_secret_arn,
        database_name=data.aurora_database_name or "docpipe",
    )

# --- exports → .env (see /.env.example) ---
pulumi.export("documents_bucket", data.documents_bucket)
pulumi.export("jobs_table", data.jobs_table)
pulumi.export("conversations_table", data.conversations_table)
pulumi.export("queue_url", messaging.queue_url)
pulumi.export("knowledge_base_id", kb.knowledge_base_id)
pulumi.export("kb_source_bucket", kb.source_bucket)
pulumi.export("kb_data_source_id", kb.data_source_id)
pulumi.export("guardrail_id", safety.guardrail_id)
pulumi.export("guardrail_version", safety.guardrail_version)
pulumi.export("chat_model_id", chat_model_id)
pulumi.export("lambda_role_arn", iam.lambda_role_arn)
pulumi.export("worker_policy_arn", iam.worker_policy_arn)
pulumi.export("vpc_id", network.vpc.id)
pulumi.export("private_subnet_ids", network.private_subnet_ids)

# --- Aurora (only when enabled; scripts/aurora_bootstrap.py reads these) ---
if enable_aurora:
    pulumi.export("aurora_cluster_arn", data.aurora_cluster_arn)
    pulumi.export("aurora_cluster_endpoint", data.aurora_cluster_endpoint)
    pulumi.export("aurora_database_name", data.aurora_database_name)
    pulumi.export("aurora_master_secret_arn", data.aurora_master_secret_arn)
    pulumi.export("aurora_bedrock_secret_arn", data.aurora_bedrock_secret_arn)
    pulumi.export("aurora_bedrock_username", data.aurora_bedrock_username)
if aurora_kb is not None:
    # The Phase 5b config flip: retrieval.py takes knowledge_base_id, so pointing
    # the benchmark at the other store is this value, not a code change.
    pulumi.export("aurora_knowledge_base_id", aurora_kb.knowledge_base_id)
    pulumi.export("aurora_kb_data_source_id", aurora_kb.data_source_id)
