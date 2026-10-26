"""The front door: Lambda + REST API Gateway ``POST /chat`` + usage plan + API key.

The design is PLAN.md Phase 3's, locked there and honoured here:

* **REST API Gateway, not HTTP API.** Only REST carries usage plans, and the
  usage plan's *daily quota* is the hard Bedrock cost cap — a key that has spent
  its day gets 429 at the gateway, before any Lambda runs or any model bills.
* **API key + usage plan, no Cognito.** health.studio's server holds the key and
  asserts the opaque ``userId``; the browser never calls this API.
* **The key's VALUE is never an output.** ``api_key_id`` is; the value is read
  once by the human with ``aws apigateway get-api-key --include-value`` (or the
  console) and set as the app's secret. Pulumi state holds it either way — the
  export is what would put it in every ``stack output`` and every CI log.
* **29 s everywhere.** REST API Gateway's integration timeout is 29 s; the
  function's timeout matches so a slow turn fails at one layer with one message
  (504 from the gateway) instead of two. Measured agentic turns run ~14 s with
  ~4 Converse calls (FINDINGS 2026-08-20); ``CHAT_MAX_ITERATIONS`` is the bound.
* **arm64 / python3.12.** ``make lambda-zip`` builds the wheels for
  ``aarch64-manylinux2014``; pydantic-core is compiled, so the two must agree
  (``test_lambda_packaging_contract.py`` reads both).
* **Near-zero at rest.** Lambda and API Gateway are per-request, DynamoDB is
  on-demand; the retention-capped log group is the only standing resource.
* **No gateway access logging.** API Gateway logs to CloudWatch through an
  *account-level* role (``aws.apigateway.Account``) that this stack does not
  own; the Lambda's own JSON log carries the per-turn numbers instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import pulumi
import pulumi_aws as aws

# What `make lambda-zip` builds, and what the function is configured to run.
HANDLER = "chat.handler"
RUNTIME = "python3.12"
ARCHITECTURE = "arm64"


class Api(pulumi.ComponentResource):
    def __init__(
        self,
        prefix: str,
        *,
        role_arn: pulumi.Input[str],
        code_path: str,
        environment: Mapping[str, pulumi.Input[str]],
        stage_name: str = "dev",
        rate_limit: float = 5,
        burst_limit: int = 10,
        daily_quota: int = 500,
        timeout_seconds: int = 29,
        memory_mb: int = 512,
        log_retention_days: int = 14,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__("docpipe:api:Api", prefix, None, opts)
        me = pulumi.ResourceOptions(parent=self)

        function_name = f"{prefix}-chat"
        log_group = aws.cloudwatch.LogGroup(
            f"{prefix}-chat-logs",
            name=f"/aws/lambda/{function_name}",
            retention_in_days=log_retention_days,
            tags={"Name": f"{prefix}-chat-logs"},
            opts=me,
        )

        self.function = aws.lambda_.Function(
            f"{prefix}-chat",
            name=function_name,
            role=role_arn,
            runtime=RUNTIME,
            architectures=[ARCHITECTURE],
            handler=HANDLER,
            code=pulumi.FileArchive(code_path),
            timeout=timeout_seconds,
            memory_size=memory_mb,
            environment={"variables": dict(environment)},
            logging_config={"log_format": "JSON", "log_group": log_group.name},
            tags={"Name": function_name},
            opts=pulumi.ResourceOptions(parent=self, depends_on=[log_group]),
        )
        self.function_name = self.function.name

        rest = aws.apigateway.RestApi(
            f"{prefix}-api",
            name=f"{prefix}-api",
            description="docpipe chat API - REST so the usage plan can cap daily spend",
            endpoint_configuration={"types": "REGIONAL"},
            api_key_source="HEADER",
            tags={"Name": f"{prefix}-api"},
            opts=me,
        )
        chat = aws.apigateway.Resource(
            f"{prefix}-chat-resource",
            rest_api=rest.id,
            parent_id=rest.root_resource_id,
            path_part="chat",
            opts=me,
        )
        post = aws.apigateway.Method(
            f"{prefix}-chat-post",
            rest_api=rest.id,
            resource_id=chat.id,
            http_method="POST",
            authorization="NONE",
            api_key_required=True,
            opts=me,
        )
        integration = aws.apigateway.Integration(
            f"{prefix}-chat-integration",
            rest_api=rest.id,
            resource_id=chat.id,
            http_method=post.http_method,
            integration_http_method="POST",
            type="AWS_PROXY",
            uri=self.function.invoke_arn,
            opts=me,
        )
        aws.lambda_.Permission(
            f"{prefix}-chat-invoke-from-apigw",
            action="lambda:InvokeFunction",
            function=self.function.name,
            principal="apigateway.amazonaws.com",
            source_arn=pulumi.Output.concat(rest.execution_arn, "/*/POST/chat"),
            opts=me,
        )

        # A deployment is a snapshot; it must be re-cut when the routes change.
        # The trigger hashes what the snapshot depends on, so an edit to the
        # method or integration redeploys and an unrelated `up` does not.
        deployment = aws.apigateway.Deployment(
            f"{prefix}-api-deployment",
            rest_api=rest.id,
            triggers={
                "routes": pulumi.Output.all(
                    chat.path_part, post.http_method, integration.uri
                ).apply(lambda parts: hashlib.sha256(json.dumps(parts).encode()).hexdigest())
            },
            opts=pulumi.ResourceOptions(parent=self, depends_on=[post, integration]),
        )
        stage = aws.apigateway.Stage(
            f"{prefix}-api-{stage_name}",
            rest_api=rest.id,
            deployment=deployment.id,
            stage_name=stage_name,
            tags={"Name": f"{prefix}-api-{stage_name}"},
            opts=me,
        )
        aws.apigateway.MethodSettings(
            f"{prefix}-api-throttle",
            rest_api=rest.id,
            stage_name=stage.stage_name,
            method_path="*/*",
            settings={
                "throttling_rate_limit": rate_limit,
                "throttling_burst_limit": burst_limit,
                "metrics_enabled": True,
            },
            opts=me,
        )

        plan = aws.apigateway.UsagePlan(
            f"{prefix}-plan",
            name=f"{prefix}-plan",
            description="One key, one daily quota: the hard cap on Bedrock spend from the app",
            api_stages=[{"api_id": rest.id, "stage": stage.stage_name}],
            quota_settings={"limit": daily_quota, "period": "DAY"},
            throttle_settings={"rate_limit": rate_limit, "burst_limit": burst_limit},
            tags={"Name": f"{prefix}-plan"},
            opts=me,
        )
        key = aws.apigateway.ApiKey(
            f"{prefix}-key",
            name=f"{prefix}-key",
            description="health.studio's server-side key for POST /chat",
            enabled=True,
            tags={"Name": f"{prefix}-key"},
            opts=me,
        )
        aws.apigateway.UsagePlanKey(
            f"{prefix}-plan-key",
            key_id=key.id,
            key_type="API_KEY",
            usage_plan_id=plan.id,
            opts=me,
        )

        self.chat_url = pulumi.Output.concat(stage.invoke_url, "/chat")
        self.api_key_id = key.id  # the id; the value is deliberately not surfaced
        self.usage_plan_id = plan.id

        self.register_outputs({"chat_url": self.chat_url, "api_key_id": self.api_key_id})
