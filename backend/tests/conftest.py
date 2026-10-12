import boto3
import pytest

from app.aws import ec2
from app.core.config import settings


@pytest.fixture(autouse=True)
def isolated_aws_credentials_path(tmp_path, monkeypatch):
    """Every test gets its own credentials cache file, never the real one at
    ~/.maidenrun/credentials.json."""
    monkeypatch.setattr(settings, "aws_credentials_path", str(tmp_path / "credentials.json"))
    # Same for deployment history — otherwise every mocked deploy leaves a
    # fake "running" record in the real data/deployments.
    monkeypatch.setattr(settings, "deployments_dir", str(tmp_path / "deployments"))
    monkeypatch.setattr(settings, "exports_dir", str(tmp_path / "exports"))
    # Never let the AWS credential chain see the developer's real ~/.aws or
    # AWS_* env vars — tests that want a profile set one up explicitly.
    monkeypatch.setattr(settings, "aws_profile", "")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def create_instance_profile(session: boto3.Session) -> None:
    """Mirrors the one-time setup in infra/README.md: a pre-created instance role.

    Must be called from *within* an active @mock_aws-decorated test, not
    used as a plain pytest fixture — fixture setup runs before @mock_aws
    wraps the test function body, so AWS calls made there would hit real
    (unmocked) boto3 instead.
    """
    iam = session.client("iam")
    trust_policy = (
        '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow", '
        '"Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
    )
    iam.create_role(RoleName=ec2.INSTANCE_ROLE_NAME, AssumeRolePolicyDocument=trust_policy)
    iam.create_instance_profile(InstanceProfileName=ec2.INSTANCE_ROLE_NAME)
    iam.add_role_to_instance_profile(InstanceProfileName=ec2.INSTANCE_ROLE_NAME, RoleName=ec2.INSTANCE_ROLE_NAME)
