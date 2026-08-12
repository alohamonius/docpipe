"""docpipe infrastructure — dev stack entrypoint.

Wires the components and exports the values that go into .env (the same set the
old Terraform stack produced — see /.env.example). Run `pulumi stack output` to
read them after `pulumi up`.
"""

import pulumi_aws as aws
from components import Data, Iam, KnowledgeBase, Messaging, Network, Safety

import pulumi

cfg = pulumi.Config()
prefix = cfg.get("namePrefix") or "docpipe-dev"
enable_aurora = cfg.get_bool("enableAurora") or False
bedrock_model_id = cfg.get("bedrockModelId") or "us.deepseek.r1-v1:0"

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
iam = Iam(
    prefix,
    ident.account_id,
    region,
    bedrock_model_id,
    documents_bucket_arn=data.documents_bucket_arn,
    jobs_table_arn=data.jobs_table_arn,
    conversations_table_arn=data.conversations_table_arn,
    queue_arn=messaging.queue_arn,
    knowledge_base_arn=kb.knowledge_base_arn,
)
safety = Safety(prefix, ident.account_id, region)

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
pulumi.export("lambda_role_arn", iam.lambda_role_arn)
pulumi.export("worker_policy_arn", iam.worker_policy_arn)
pulumi.export("vpc_id", network.vpc.id)
pulumi.export("private_subnet_ids", network.private_subnet_ids)
