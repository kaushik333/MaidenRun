import tarfile
from io import BytesIO

import boto3
from moto import mock_aws

from app.aws.s3_bundle import build_bundle, delete_bundle, ensure_bundle_bucket, upload_bundle


def _session(region: str = "us-east-1") -> boto3.Session:
    return boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=region)


@mock_aws
def test_ensure_bundle_bucket_creates_and_reuses_bucket_in_us_east_1() -> None:
    session = _session("us-east-1")
    bucket = ensure_bundle_bucket(session)

    account_id = session.client("sts").get_caller_identity()["Account"]
    assert bucket == f"maidenrun-{account_id}"

    # Calling again must not raise (e.g. re-creating an existing bucket).
    assert ensure_bundle_bucket(session) == bucket


@mock_aws
def test_ensure_bundle_bucket_works_in_a_non_us_east_1_region() -> None:
    session = _session("eu-west-1")
    bucket = ensure_bundle_bucket(session)
    location = session.client("s3").get_bucket_location(Bucket=bucket)["LocationConstraint"]
    assert location == "eu-west-1"


@mock_aws
def test_ensure_bundle_bucket_sets_an_expiration_lifecycle_rule() -> None:
    session = _session()
    bucket = ensure_bundle_bucket(session)
    rules = session.client("s3").get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    assert len(rules) == 1
    assert rules[0]["Expiration"]["Days"] == 1
    assert rules[0]["Filter"]["Prefix"] == "feedback-stack-bundles/"


def test_build_bundle_includes_source_and_uploaded_questionnaire() -> None:
    data = build_bundle('{"questions": []}')

    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        assert "docker-compose.yml" in names
        assert "backend/app/main.py" in names
        assert "feedback-questions.json" in names

        questions_member = tar.extractfile("feedback-questions.json")
        assert questions_member.read() == b'{"questions": []}'


def test_build_bundle_excludes_dev_artifacts() -> None:
    data = build_bundle("{}")
    with tarfile.open(fileobj=BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        assert not any(".venv" in n for n in names)
        assert not any("node_modules" in n for n in names)
        assert not any(n.startswith("backend/tests/") for n in names)
        assert ".env.example" not in names
        assert "feedback-questions.example.json" not in names


@mock_aws
def test_upload_and_delete_bundle_round_trip() -> None:
    session = _session()
    bucket = ensure_bundle_bucket(session)
    key = "feedback-stack-bundles/test.tar.gz"

    upload_bundle(session, bucket, key, b"fake tarball bytes")
    objects = session.client("s3").list_objects_v2(Bucket=bucket).get("Contents", [])
    assert [o["Key"] for o in objects] == [key]

    delete_bundle(session, bucket, key)
    objects = session.client("s3").list_objects_v2(Bucket=bucket).get("Contents", [])
    assert objects == []


@mock_aws
def test_delete_bundle_does_not_raise_when_object_already_gone() -> None:
    session = _session()
    bucket = ensure_bundle_bucket(session)
    delete_bundle(session, bucket, "feedback-stack-bundles/does-not-exist.tar.gz")
