"""Pulumi dynamic provider for S3 Vectors (bucket + index).

pulumi-aws (7.40) has no ``s3vectors`` resource, so this small boto3-backed
dynamic provider creates the vector bucket + one index at deploy time and exposes
their ARNs for the Bedrock KB to reference. Everything here is immutable, so any
input change triggers a replace (delete-before-replace — bucket names are unique).

boto3 is imported inside the methods because the provider is serialized by Pulumi.
"""

from __future__ import annotations

import contextlib

from pulumi.dynamic import CreateResult, DiffResult, Resource, ResourceProvider

import pulumi

# Every input whose change must force a replace. `non_filterable_metadata_keys`
# belongs here and the omission would be silent: the provider has no `update()`,
# so a key missing from this tuple makes `diff()` return `changes=False` and
# Pulumi reports "unchanged" while the index keeps its old metadata config.
_KEYS = (
    "region",
    "vector_bucket_name",
    "index_name",
    "dimension",
    "distance_metric",
    "data_type",
    "non_filterable_metadata_keys",
)


class _S3VectorsProvider(ResourceProvider):
    def _client(self, props: dict):
        import boto3

        # aws:profile only configures pulumi-aws; boto3 here needs it explicitly.
        session = boto3.Session(profile_name=props.get("profile") or None)
        return session.client("s3vectors", region_name=props["region"])

    def create(self, props: dict) -> CreateResult:
        c = self._client(props)
        bucket, index = props["vector_bucket_name"], props["index_name"]

        # A bucket left behind by an earlier failed create is adopted, not an error;
        # names are deterministic and stack-owned, and delete() still owns cleanup.
        with contextlib.suppress(c.exceptions.ConflictException):
            c.create_vector_bucket(vectorBucketName=bucket)
        bucket_arn = c.get_vector_bucket(vectorBucketName=bucket)["vectorBucket"]["vectorBucketArn"]
        # Non-filterable keys can ONLY be declared here. The API (botocore
        # 1.43.62) has CreateIndex / DeleteIndex / GetIndex / ListIndexes and no
        # UpdateIndex — verified against the service model, not assumed — so the
        # set is immutable and getting it wrong costs a delete-and-reingest.
        kwargs = {}
        if props.get("non_filterable_metadata_keys"):
            kwargs["metadataConfiguration"] = {
                "nonFilterableMetadataKeys": list(props["non_filterable_metadata_keys"])
            }
        c.create_index(
            vectorBucketName=bucket,
            indexName=index,
            dataType=props["data_type"],
            # props JSON-roundtrip through the engine, so ints arrive as floats
            dimension=int(props["dimension"]),
            distanceMetric=props["distance_metric"],
            **kwargs,
        )
        index_arn = c.get_index(vectorBucketName=bucket, indexName=index)["index"]["indexArn"]

        return CreateResult(
            id_=f"{bucket}/{index}",
            outs={**props, "vector_bucket_arn": bucket_arn, "index_arn": index_arn},
        )

    def delete(self, id_: str, props: dict) -> None:
        from botocore.exceptions import ClientError

        c = self._client(props)
        bucket = props["vector_bucket_name"]
        for call in (
            lambda: c.delete_index(vectorBucketName=bucket, indexName=props["index_name"]),
            lambda: c.delete_vector_bucket(vectorBucketName=bucket),
        ):
            # Teardown is best-effort: an already-deleted resource is a success.
            with contextlib.suppress(ClientError):
                call()

    def diff(self, id_: str, olds: dict, news: dict) -> DiffResult:
        changed = [k for k in _KEYS if olds.get(k) != news.get(k)]
        return DiffResult(changes=bool(changed), replaces=changed, delete_before_replace=True)


class S3VectorsIndex(Resource):
    """One S3 Vectors bucket + index. Outputs: vector_bucket_arn, index_arn.

    ``non_filterable_metadata_keys`` is the one input you cannot fix later. S3
    Vectors caps **filterable** metadata at 2 KB per vector (40 KB total), and
    Bedrock stores the chunk body in the vector as ``AMAZON_BEDROCK_TEXT`` — so
    an index that declares nothing non-filterable fails ingestion for every
    chunk over ~2 KB, which on health.studio's corpus is 243 of 383.
    """

    vector_bucket_arn: pulumi.Output[str]
    index_arn: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        region: str,
        vector_bucket_name: str,
        index_name: str,
        dimension: int = 1024,
        distance_metric: str = "cosine",
        data_type: str = "float32",
        non_filterable_metadata_keys: list[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(
            _S3VectorsProvider(),
            name,
            {
                "region": region,
                # operational, not identity — deliberately not in _KEYS/diff
                "profile": pulumi.Config("aws").get("profile"),
                "vector_bucket_name": vector_bucket_name,
                "index_name": index_name,
                "dimension": dimension,
                "distance_metric": distance_metric,
                "data_type": data_type,
                "non_filterable_metadata_keys": non_filterable_metadata_keys or [],
                # declared outputs — populated by create()
                "vector_bucket_arn": None,
                "index_arn": None,
            },
            opts,
        )
