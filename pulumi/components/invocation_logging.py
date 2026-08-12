"""Pulumi dynamic provider for Bedrock model invocation logging (account singleton).

pulumi-aws (7.40) has no resource for this, so this boto3-backed dynamic provider
sets it via ``put_model_invocation_logging_configuration`` on create/update and
clears it on delete. Text only — image/embedding/video delivery off. It's an
account+region singleton, so there is at most one of these.
"""

from __future__ import annotations

import contextlib

from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider, UpdateResult

import pulumi

_KEYS = ("region", "bucket_name", "log_group_name", "role_arn")


class _InvocationLoggingProvider(ResourceProvider):
    def _client(self, props: dict):
        import boto3

        # aws:profile only configures pulumi-aws; boto3 here needs it explicitly.
        session = boto3.Session(profile_name=props.get("profile") or None)
        return session.client("bedrock", region_name=props["region"])

    def _put(self, props: dict) -> None:
        self._client(props).put_model_invocation_logging_configuration(
            loggingConfig={
                "textDataDeliveryEnabled": True,
                "imageDataDeliveryEnabled": False,
                "embeddingDataDeliveryEnabled": False,
                "videoDataDeliveryEnabled": False,
                "s3Config": {"bucketName": props["bucket_name"], "keyPrefix": "invocations/"},
                "cloudWatchConfig": {
                    "logGroupName": props["log_group_name"],
                    "roleArn": props["role_arn"],
                    "largeDataDeliveryS3Config": {
                        "bucketName": props["bucket_name"],
                        "keyPrefix": "large/",
                    },
                },
            }
        )

    def create(self, props: dict) -> CreateResult:
        self._put(props)
        return CreateResult(id_=f"{props['region']}-bedrock-invocation-logging", outs=props)

    def update(self, id_: str, olds: dict, news: dict) -> UpdateResult:
        self._put(news)
        return UpdateResult(outs=news)

    def delete(self, id_: str, props: dict) -> None:
        from botocore.exceptions import ClientError

        # Teardown is best-effort: an already-cleared config is a success.
        with contextlib.suppress(ClientError):
            self._client(props).delete_model_invocation_logging_configuration()

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        changed = [k for k in _KEYS if olds.get(k) != news.get(k)]
        # Region change means a different singleton → replace; else update in place.
        replaces = ["region"] if olds.get("region") != news.get("region") else []
        return DiffResult(changes=bool(changed), replaces=replaces)


class InvocationLogging(Resource):
    def __init__(
        self,
        name: str,
        *,
        region: str,
        bucket_name: pulumi.Input[str],
        log_group_name: pulumi.Input[str],
        role_arn: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            _InvocationLoggingProvider(),
            name,
            {
                "region": region,
                # operational, not identity — deliberately not in _KEYS/diff
                "profile": pulumi.Config("aws").get("profile"),
                "bucket_name": bucket_name,
                "log_group_name": log_group_name,
                "role_arn": role_arn,
            },
            opts,
        )
