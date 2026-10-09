import base64
import urllib.error
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from app.aws import ec2
from app.core.config import settings
from tests.conftest import create_instance_profile

DEPLOYMENT_ID = "dep123456789"
REPO_URL = "https://github.com/kaushik/agent"
SECRETS = {"OPENAI_API_KEY": "sk-should-never-appear-in-userdata"}


@pytest.fixture
def aws_session():
    return boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )


@mock_aws
def test_ensure_app_security_group_opens_only_8001(aws_session):
    group_id = ec2.ensure_app_security_group(aws_session)

    group = aws_session.client("ec2").describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    assert len(group["IpPermissions"]) == 1
    perm = group["IpPermissions"][0]
    assert perm["FromPort"] == perm["ToPort"] == 8001
    assert perm["IpRanges"] == [{"CidrIp": "0.0.0.0/0"}]


@mock_aws
def test_ensure_app_security_group_raises_clear_error_without_default_vpc(aws_session, monkeypatch):
    # moto's default VPC has real subnets attached and refuses a plain delete
    # (matching AWS), so the "no default VPC" case is simulated at the call
    # level instead: describe_vpcs is stubbed to report none, everything else
    # on the client behaves normally.
    real_client = aws_session.client("ec2")
    stub_client = MagicMock(wraps=real_client)
    stub_client.describe_vpcs.return_value = {"Vpcs": []}
    monkeypatch.setattr(aws_session, "client", lambda *args, **kwargs: stub_client)

    with pytest.raises(ec2.Ec2LaunchError, match="No default VPC"):
        ec2.ensure_app_security_group(aws_session)


@mock_aws
def test_ensure_app_security_group_is_idempotent(aws_session):
    first = ec2.ensure_app_security_group(aws_session)
    second = ec2.ensure_app_security_group(aws_session)
    assert first == second


@mock_aws
def test_write_secret_parameters_stores_securestring(aws_session):
    ec2.write_secret_parameters(aws_session, DEPLOYMENT_ID, SECRETS)

    param = aws_session.client("ssm").get_parameter(
        Name=f"/maidenrun/{DEPLOYMENT_ID}/OPENAI_API_KEY", WithDecryption=True
    )
    assert param["Parameter"]["Value"] == SECRETS["OPENAI_API_KEY"]
    assert param["Parameter"]["Type"] == "SecureString"


def test_render_user_data_never_contains_raw_secret_value():
    user_data = ec2.render_user_data(
        repo_url=REPO_URL,
        entry_point="compose",
        secret_names=list(SECRETS.keys()),
        deployment_id=DEPLOYMENT_ID,
        region=settings.aws_default_region,
    )

    assert SECRETS["OPENAI_API_KEY"] not in user_data
    assert "OPENAI_API_KEY" in user_data
    assert "aws ssm get-parameter" in user_data
    assert "--with-decryption" in user_data


def test_render_user_data_with_repo_token_never_contains_raw_token():
    user_data = ec2.render_user_data(
        repo_url=REPO_URL,
        entry_point="compose",
        secret_names=[],
        deployment_id=DEPLOYMENT_ID,
        region=settings.aws_default_region,
        needs_repo_token=True,
    )

    assert "aws ssm get-parameter" in user_data
    assert f"{ec2.PARAMETER_NAMESPACE}/{DEPLOYMENT_ID}/{ec2.REPO_TOKEN_PARAM_NAME}" in user_data
    assert 'git clone --depth 1 "https://${REPO_TOKEN}@github.com/kaushik/agent"' in user_data


def test_render_user_data_without_repo_token_clones_plain_url():
    user_data = ec2.render_user_data(
        repo_url=REPO_URL,
        entry_point="compose",
        secret_names=[],
        deployment_id=DEPLOYMENT_ID,
        region=settings.aws_default_region,
    )

    assert f'git clone --depth 1 "{REPO_URL}"' in user_data
    assert "REPO_TOKEN" not in user_data


def test_render_user_data_compose_runs_docker_compose_up():
    user_data = ec2.render_user_data(
        repo_url=REPO_URL, entry_point="compose", secret_names=[], deployment_id=DEPLOYMENT_ID, region="us-east-1"
    )
    assert "docker compose up -d" in user_data
    assert "compose.env.yml" not in user_data  # no secrets, no override needed


def test_render_user_data_compose_injects_secret_names_via_override():
    user_data = ec2.render_user_data(
        repo_url=REPO_URL,
        entry_point="compose",
        secret_names=list(SECRETS.keys()),
        deployment_id=DEPLOYMENT_ID,
        region="us-east-1",
    )

    assert 'echo "      - OPENAI_API_KEY"' in user_data  # bare name, value comes from the shell
    assert SECRETS["OPENAI_API_KEY"] not in user_data
    assert user_data.rstrip().endswith(
        'docker compose -f "$COMPOSE_MAIN" -f /opt/maidenrun/compose.env.yml up -d'
    )


def test_render_user_data_dockerfile_builds_and_runs_with_bare_env_flags():
    user_data = ec2.render_user_data(
        repo_url=REPO_URL,
        entry_point="dockerfile",
        secret_names=["OPENAI_API_KEY"],
        deployment_id=DEPLOYMENT_ID,
        region="us-east-1",
    )

    assert "docker build -t agent-app ." in user_data
    run_line = next(line for line in user_data.splitlines() if line.startswith("docker run"))
    assert "-e OPENAI_API_KEY" in run_line
    assert "OPENAI_API_KEY=" not in run_line  # bare flag, value comes from the shell, not the command line


@mock_aws
def test_latest_al2023_ami_returns_id_and_root_device(aws_session):
    ami_id, root_device = ec2.latest_al2023_ami(aws_session)
    assert ami_id.startswith("ami-")
    assert root_device


@mock_aws
def test_launch_app_instance_returns_instance_id_and_public_ip(aws_session):
    create_instance_profile(aws_session)
    result = ec2.launch_app_instance(
        aws_session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
    )

    assert result["instance_id"].startswith("i-")
    assert result["public_ip"]


def _name_tag(client, instance_id: str) -> str | None:
    instance = client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return next((t["Value"] for t in instance["Tags"] if t["Key"] == "Name"), None)


@mock_aws
def test_launch_app_instance_uses_the_given_name_tag(aws_session):
    create_instance_profile(aws_session)
    result = ec2.launch_app_instance(
        aws_session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
        instance_name="my-agent",
    )

    assert _name_tag(aws_session.client("ec2"), result["instance_id"]) == "my-agent"


@mock_aws
def test_launch_app_instance_falls_back_to_a_default_name(aws_session):
    create_instance_profile(aws_session)
    result = ec2.launch_app_instance(
        aws_session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
    )

    # Never nameless in the console, even if the user left it blank.
    assert _name_tag(aws_session.client("ec2"), result["instance_id"]) == f"maidenrun-{DEPLOYMENT_ID}"


@mock_aws
def test_terminate_instance_terminates_the_given_instance(aws_session):
    create_instance_profile(aws_session)
    result = ec2.launch_app_instance(
        aws_session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
    )

    ec2.terminate_instance(aws_session, result["instance_id"])

    client = aws_session.client("ec2")
    instance = client.describe_instances(InstanceIds=[result["instance_id"]])["Reservations"][0]["Instances"][0]
    assert instance["State"]["Name"] in ("shutting-down", "terminated")


@mock_aws
def test_launch_app_instance_bakes_the_sessions_own_region_into_user_data():
    # Regression test: this used to hardcode settings.aws_default_region into
    # the user-data script regardless of what region the instance actually
    # launched into, so a non-default-region deploy would have its boot
    # script fetch SSM secrets from the wrong region and fail.
    non_default_region = "eu-west-1"
    assert non_default_region != settings.aws_default_region
    session = boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=non_default_region)
    create_instance_profile(session)

    result = ec2.launch_app_instance(
        session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
    )

    attr = session.client("ec2").describe_instance_attribute(
        InstanceId=result["instance_id"], Attribute="userData"
    )
    user_data = base64.b64decode(attr["UserData"]["Value"]).decode()
    assert f"--region {non_default_region}" in user_data
    assert f"--region {settings.aws_default_region}" not in user_data


@mock_aws
def test_launch_app_instance_security_group_has_only_8001(aws_session):
    create_instance_profile(aws_session)
    result = ec2.launch_app_instance(
        aws_session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
    )

    client = aws_session.client("ec2")
    instance = client.describe_instances(InstanceIds=[result["instance_id"]])["Reservations"][0]["Instances"][0]
    sg_ids = [sg["GroupId"] for sg in instance["SecurityGroups"]]
    group = client.describe_security_groups(GroupIds=sg_ids)["SecurityGroups"][0]
    assert len(group["IpPermissions"]) == 1
    assert group["IpPermissions"][0]["FromPort"] == 8001


@mock_aws
def test_launch_app_instance_stores_repo_token_separately_from_secrets(aws_session):
    create_instance_profile(aws_session)
    ec2.launch_app_instance(
        aws_session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
        github_token="ghp_secrettoken",
    )

    ssm = aws_session.client("ssm")
    token_param = ssm.get_parameter(
        Name=f"/maidenrun/{DEPLOYMENT_ID}/{ec2.REPO_TOKEN_PARAM_NAME}", WithDecryption=True
    )
    assert token_param["Parameter"]["Value"] == "ghp_secrettoken"

    # It must never be exported into the agent's own process environment.
    instance = aws_session.client("ec2").describe_instances()["Reservations"][0]["Instances"][0]
    user_data = base64.b64decode(
        aws_session.client("ec2")
        .describe_instance_attribute(InstanceId=instance["InstanceId"], Attribute="userData")["UserData"]["Value"]
    ).decode()
    assert f"export {ec2.REPO_TOKEN_PARAM_NAME}" not in user_data
    assert "ghp_secrettoken" not in user_data


@mock_aws
def test_launch_app_instance_writes_secret_before_launch(aws_session):
    create_instance_profile(aws_session)
    ec2.launch_app_instance(
        aws_session,
        deployment_id=DEPLOYMENT_ID,
        repo_url=REPO_URL,
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets=SECRETS,
    )

    param = aws_session.client("ssm").get_parameter(
        Name=f"/maidenrun/{DEPLOYMENT_ID}/OPENAI_API_KEY", WithDecryption=True
    )
    assert param["Parameter"]["Value"] == SECRETS["OPENAI_API_KEY"]


def test_wait_for_port_health_succeeds_on_2xx_response(monkeypatch):
    monkeypatch.setattr(ec2.urllib.request, "urlopen", lambda url, timeout: object())
    ec2.wait_for_port_health("1.2.3.4", 8001, timeout_seconds=5)  # raises on failure, nothing to assert on success


def test_wait_for_port_health_succeeds_on_non_2xx_response(monkeypatch):
    # A 404/500 from the user's own agent still means *something* is up and answering.
    def raise_http_error(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(ec2.urllib.request, "urlopen", raise_http_error)
    ec2.wait_for_port_health("1.2.3.4", 8001, timeout_seconds=5)


def test_wait_for_port_health_retries_then_succeeds(monkeypatch):
    attempts = {"count": 0}

    def flaky_urlopen(url, timeout):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise urllib.error.URLError("connection refused")
        return object()

    monkeypatch.setattr(ec2.urllib.request, "urlopen", flaky_urlopen)
    ec2.wait_for_port_health("1.2.3.4", 8001, timeout_seconds=5, poll_interval_seconds=0.01)
    assert attempts["count"] == 3


def test_wait_for_port_health_raises_clear_error_on_timeout(monkeypatch):
    def always_refused(url, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ec2.urllib.request, "urlopen", always_refused)
    with pytest.raises(ec2.Ec2LaunchError, match="did not respond on port 8001"):
        ec2.wait_for_port_health("1.2.3.4", 8001, timeout_seconds=0.05, poll_interval_seconds=0.01)


def test_wait_for_port_health_works_for_other_ports(monkeypatch):
    monkeypatch.setattr(ec2.urllib.request, "urlopen", lambda url, timeout: object())
    ec2.wait_for_port_health("1.2.3.4", 8002, timeout_seconds=5)  # no exception raised for a non-8001 port either
