import boto3
import pytest
from moto import mock_aws

REGION = "us-east-1"


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    credential_vars = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "AWS_SESSION_TOKEN",
    )
    for var in credential_vars:
        monkeypatch.setenv(var, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def s3_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket="docpipe-test")
        yield client


@pytest.fixture
def jobs_table():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.create_table(
            TableName="docpipe-jobs",
            KeySchema=[{"AttributeName": "jobId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "jobId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource


@pytest.fixture
def sqs_queue():
    with mock_aws():
        client = boto3.client("sqs", region_name=REGION)
        url = client.create_queue(QueueName="docpipe-jobs")["QueueUrl"]
        yield client, url
