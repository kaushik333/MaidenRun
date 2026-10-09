import boto3
from moto import mock_aws

from app.aws.feedback_stack import launch_feedback_stack, render_feedback_user_data
from app.core.config import settings
from tests.conftest import create_instance_profile


def test_render_feedback_user_data_fetches_both_secrets_from_ssm_and_pulls_bundle():
    script = render_feedback_user_data(deployment_id="dep123", region="us-east-1", bucket="my-bucket", key="my-key.tar.gz")

    assert "POSTGRES_PASSWORD" in script
    assert "DASHBOARD_TOKEN" in script
    assert "aws ssm get-parameter" in script
    # Neither secret's actual value ever appears — only the parameter names
    # and the command used to fetch them at boot.
    assert "dep123/POSTGRES_PASSWORD" in script
    assert "dep123/DASHBOARD_TOKEN" in script
    assert "aws s3 cp s3://my-bucket/my-key.tar.gz" in script
    assert "docker compose up -d" in script


@mock_aws
def test_launch_feedback_stack_writes_dashboard_token_to_ssm():
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region)
    create_instance_profile(session)

    result = launch_feedback_stack(session, deployment_id="dep456", bucket="my-bucket", key="my-key.tar.gz")

    assert result["dashboard_token"]
    param = session.client("ssm").get_parameter(
        Name=f"/maidenrun/dep456/DASHBOARD_TOKEN", WithDecryption=True
    )
    assert param["Parameter"]["Value"] == result["dashboard_token"]
    assert param["Parameter"]["Type"] == "SecureString"
