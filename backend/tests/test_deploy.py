import time
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from moto import mock_aws

from app.aws.credentials import NoCredentialsError, get_session
from app.aws.ec2 import Ec2LaunchError, HealthCheckTimeoutError
from app.core.config import settings
from app.main import app
from app.validation.validator import ValidateRepoResponse
from tests.conftest import create_instance_profile

CORS_ORIGIN_HEADERS = {"Origin": "http://localhost:5173"}

client = TestClient(app)

AWS = {"access_key_id": "AKIA_TEST", "secret_access_key": "shh"}

EXTERNAL_LLM_PAYLOAD = {
    "repo_url": "https://github.com/kaushik/agent",
    "aws": AWS,
    "instance_type": "t3.small",
    "ebs_size_gb": 20,
    "api_key_name": "OPENAI_API_KEY",
    "api_key_value": "sk-test",
    "env_vars": [{"key": "PINECONE_API_KEY", "value": "pc-test"}],
}

VALID_COMPOSE_REPO = ValidateRepoResponse(valid=True, entry_point="compose")


def _bootstrap_moto_account() -> None:
    """Pre-creates the instance role infra/README.md asks the user to set up
    once by hand — deploy() references it by name via app.aws.ec2."""
    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    create_instance_profile(session)


def _no_op_health(*_args, **_kwargs) -> None:
    """Drop-in replacement for wait_for_port_health that always succeeds instantly."""
    return None


def _poll_until_terminal(deployment_id: str, timeout_seconds: float = 5.0) -> dict:
    """TestClient runs BackgroundTasks to completion before client.post()
    returns, so the first poll is normally already terminal — this loop
    only exists so the tests don't depend on that TestClient implementation
    detail holding forever."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = client.get(f"/deploy/{deployment_id}/status").json()
        if status.get("result") is not None or status.get("error") is not None:
            return status
        time.sleep(0.05)
    raise TimeoutError(f"deployment {deployment_id} never reached a terminal state")


def _deploy_and_wait(payload: dict, headers: dict | None = None) -> tuple[object, dict]:
    response = client.post("/deploy", json=payload, headers=headers or {})
    assert response.status_code == 202, response.text
    status = _poll_until_terminal(response.json()["deployment_id"])
    return response, status


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_with_base_url_override_accepted(_mock_validate, _mock_health) -> None:
    _bootstrap_moto_account()
    payload = {
        **EXTERNAL_LLM_PAYLOAD,
        "base_url_name": "OPENAI_BASE_URL",
        "base_url_value": "https://my-self-hosted-llm.example.com/v1",
    }
    _response, status = _deploy_and_wait(payload)
    assert status["error"] is None
    assert status["result"] is not None


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_with_no_llm_config_accepted(_mock_validate, _mock_health) -> None:
    """Every LLM-connection field is optional — a repo that doesn't need any
    of them (already configured elsewhere, or not LLM-based at all) must
    still be deployable."""
    _bootstrap_moto_account()
    payload = {
        "repo_url": "https://github.com/kaushik/agent",
        "aws": AWS,
        "instance_type": "t3.small",
        "ebs_size_gb": 20,
    }
    _response, status = _deploy_and_wait(payload)
    assert status["error"] is None
    assert status["result"] is not None


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_passes_github_token_to_validate_repo(mock_validate, _mock_health) -> None:
    _bootstrap_moto_account()
    payload = {**EXTERNAL_LLM_PAYLOAD, "github_token": "ghp_secrettoken"}
    _deploy_and_wait(payload)
    mock_validate.assert_called_once_with(payload["repo_url"], "ghp_secrettoken")


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_stores_github_token_separately_and_never_in_history(_mock_validate, _mock_health) -> None:
    _bootstrap_moto_account()
    payload = {**EXTERNAL_LLM_PAYLOAD, "github_token": "ghp_secrettoken"}
    _response, status = _deploy_and_wait(payload)
    instance_id = status["result"]["app_instance_id"]

    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    from app.aws.ec2 import REPO_TOKEN_PARAM_NAME

    deployment_id = _response.json()["deployment_id"]
    token_param = session.client("ssm").get_parameter(
        Name=f"/maidenrun/{deployment_id}/{REPO_TOKEN_PARAM_NAME}", WithDecryption=True
    )
    assert token_param["Parameter"]["Value"] == "ghp_secrettoken"

    user_data = session.client("ec2").describe_instance_attribute(InstanceId=instance_id, Attribute="userData")
    import base64

    decoded = base64.b64decode(user_data["UserData"]["Value"]).decode()
    assert "ghp_secrettoken" not in decoded

    history_response = client.get("/deployments")
    for record in history_response.json():
        assert "ghp_secrettoken" not in str(record)


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_launches_app_instance_and_returns_real_link(_mock_validate, mock_health) -> None:
    _bootstrap_moto_account()
    _response, status = _deploy_and_wait(EXTERNAL_LLM_PAYLOAD)
    assert status["error"] is None
    result = status["result"]

    assert result["app_url"].startswith("http://")
    assert result["app_url"].endswith(":8001")
    assert result["app_instance_id"].startswith("i-")

    assert all(step == "done" for step in status["steps"].values())
    assert mock_health.call_count == 1  # just :8001


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_sets_the_given_instance_name(_mock_validate, _mock_health) -> None:
    _bootstrap_moto_account()
    payload = {**EXTERNAL_LLM_PAYLOAD, "instance_name": "my-agent"}
    _response, status = _deploy_and_wait(payload)
    result = status["result"]

    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    ec2_client = session.client("ec2")
    instance = ec2_client.describe_instances(InstanceIds=[result["app_instance_id"]])["Reservations"][0][
        "Instances"
    ][0]
    name_tag = next(t["Value"] for t in instance["Tags"] if t["Key"] == "Name")
    assert name_tag == "my-agent"


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
@patch("app.api.deploy.get_session", side_effect=get_session)
def test_deploy_passes_the_chosen_region_to_get_session(_mock_get_session, _mock_validate, _mock_health) -> None:
    _bootstrap_moto_account()
    payload = {**EXTERNAL_LLM_PAYLOAD, "aws": {**AWS, "region": "eu-west-1"}}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 202
    _mock_get_session.assert_called_once_with("AKIA_TEST", "shh", region="eu-west-1")


@mock_aws
@patch("app.api.deploy.validate_repo", return_value=ValidateRepoResponse(valid=False, error="No entry point found."))
def test_deploy_rejects_invalid_repo_before_touching_aws(_mock_validate) -> None:
    response = client.post("/deploy", json=EXTERNAL_LLM_PAYLOAD)
    assert response.status_code == 400
    assert "No entry point found" in response.json()["detail"]


@mock_aws
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_rolls_back_app_instance_when_health_check_never_passes(_mock_validate) -> None:
    """The instance launched fine but never actually came up healthy — an
    unhealthy, orphaned instance shouldn't be left running."""
    _bootstrap_moto_account()

    with patch(
        "app.api.deploy.wait_for_port_health",
        side_effect=HealthCheckTimeoutError("did not respond on port 8001 within 300s"),
    ):
        response = client.post("/deploy", json=EXTERNAL_LLM_PAYLOAD)
        status = _poll_until_terminal(response.json()["deployment_id"])

    assert status["result"] is None
    assert "did not respond on port 8001" in status["error"]
    assert status["steps"]["app_health"] == "error"

    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    instances = session.client("ec2").describe_instances()["Reservations"]
    states = [i["State"]["Name"] for r in instances for i in r["Instances"]]
    assert states == ["shutting-down"] or states == ["terminated"]


@patch(
    "app.api.deploy.get_session",
    side_effect=NoCredentialsError("No AWS credentials cached yet and none were provided."),
)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_returns_401_with_cors_headers_when_no_credentials(_mock_validate, _mock_get_session) -> None:
    response = client.post("/deploy", json=EXTERNAL_LLM_PAYLOAD, headers=CORS_ORIGIN_HEADERS)
    assert response.status_code == 401
    assert "No AWS credentials cached" in response.json()["detail"]
    # The actual bug this guards against: an *unhandled* exception's 500 response
    # skips CORSMiddleware entirely, the browser blocks it, and fetch() throws a
    # generic network error — which the frontend then shows as "could not reach
    # the backend," hiding whatever the real problem was. A handled HTTPException
    # must not have this problem. Only get_session()'s own check can raise this —
    # it never touches AWS, so it's still synchronous, unlike every AWS-side
    # failure below, which is now reported through polling instead.
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


@mock_aws
@patch(
    "app.api.deploy.launch_app_instance",
    side_effect=ClientError(
        {
            "Error": {
                "Code": "UnrecognizedClientException",
                "Message": "The security token included in the request is invalid.",
            }
        },
        "PutParameter",
    ),
)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_reports_it_when_aws_rejects_credentials(_mock_validate, _mock_launch) -> None:
    _bootstrap_moto_account()
    response = client.post("/deploy", json=EXTERNAL_LLM_PAYLOAD)
    assert response.status_code == 202
    status = _poll_until_terminal(response.json()["deployment_id"])
    assert "security token" in status["error"]
    assert status["steps"]["app_compute"] == "error"


@mock_aws
@patch(
    "app.api.deploy.launch_app_instance",
    side_effect=ClientError(
        {
            "Error": {
                "Code": "InvalidParameterValue",
                "Message": "Value (maidenrun-instance-role) for parameter "
                "iamInstanceProfile.name is invalid. Invalid IAM Instance Profile name",
            }
        },
        "RunInstances",
    ),
)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_does_not_blame_credentials_for_unrelated_aws_errors(_mock_validate, _mock_launch) -> None:
    # A bad/missing instance profile is a setup problem, not a bad access key —
    # mislabeling it "credentials" (as this used to) sends the user chasing the
    # wrong fix.
    _bootstrap_moto_account()
    response = client.post("/deploy", json=EXTERNAL_LLM_PAYLOAD)
    assert response.status_code == 202
    status = _poll_until_terminal(response.json()["deployment_id"])
    assert "credentials" not in status["error"].lower()
    assert "Invalid IAM Instance Profile name" in status["error"]


@mock_aws
@patch("app.api.deploy.launch_app_instance", side_effect=Ec2LaunchError("No default VPC found in this account/region."))
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_reports_it_when_provisioning_fails(_mock_validate, _mock_launch) -> None:
    _bootstrap_moto_account()
    response = client.post("/deploy", json=EXTERNAL_LLM_PAYLOAD)
    assert response.status_code == 202
    status = _poll_until_terminal(response.json()["deployment_id"])
    assert "No default VPC" in status["error"]
    assert status["steps"]["app_compute"] == "error"


def test_deploy_status_returns_404_for_unknown_deployment_id() -> None:
    response = client.get("/deploy/does-not-exist/status")
    assert response.status_code == 404


def test_deploy_api_key_name_without_value_rejected() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "api_key_name": "OPENAI_API_KEY", "api_key_value": None}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_api_key_value_without_name_rejected() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "api_key_name": None, "api_key_value": "sk-test"}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_base_url_name_without_value_rejected() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "base_url_name": "OPENAI_BASE_URL", "base_url_value": None}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_base_url_value_without_name_rejected() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "base_url_name": None, "base_url_value": "https://example.com/v1"}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_rejects_env_var_names_that_are_not_valid_shell_names() -> None:
    for bad in ("MY-KEY", "1KEY", 'KEY"; rm -rf /', "MY KEY"):
        payload = {**EXTERNAL_LLM_PAYLOAD, "env_vars": [{"key": bad, "value": "x"}]}
        response = client.post("/deploy", json=payload)
        assert response.status_code == 422, bad


def test_deploy_rejects_duplicate_env_var_names() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "env_vars": [{"key": "OPENAI_API_KEY", "value": "x"}]}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_rejects_reserved_repo_token_name() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "env_vars": [{"key": "__repo_token", "value": "x"}]}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_rejects_gpu_instance_type() -> None:
    # GPU instances no longer have any path to be provisioned — hosting an
    # LLM is entirely the user's own responsibility now, wherever they do it.
    payload = {**EXTERNAL_LLM_PAYLOAD, "instance_type": "g4dn.xlarge"}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_rejects_invalid_ebs_size() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "ebs_size_gb": 999}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


def test_deploy_rejects_missing_repo_url() -> None:
    payload = {**EXTERNAL_LLM_PAYLOAD, "repo_url": ""}
    response = client.post("/deploy", json=payload)
    assert response.status_code == 422


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_cost_estimate_reflects_real_instance_type_and_launch_time(_mock_validate, _mock_health) -> None:
    _bootstrap_moto_account()
    _response, status = _deploy_and_wait(EXTERNAL_LLM_PAYLOAD)
    deployment_id = _response.json()["deployment_id"]

    response = client.get(f"/deploy/{deployment_id}/cost-estimate")
    assert response.status_code == 200
    body = response.json()
    assert body["instance_type"] == EXTERNAL_LLM_PAYLOAD["instance_type"]
    assert body["hourly_rate"] > 0
    assert body["estimated_usd"] >= 0
    assert body["launched_at"] == status["result"]["launched_at"]


def test_deploy_cost_estimate_returns_404_for_unknown_deployment_id() -> None:
    response = client.get("/deploy/does-not-exist/cost-estimate")
    assert response.status_code == 404


@mock_aws
def test_terminate_uses_cached_credentials_no_repaste_needed() -> None:
    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    create_instance_profile(session)
    # Seed the local credential cache exactly as /deploy would have on first use.
    get_session("AKIA_TEST", "shh", region=settings.aws_default_region)

    from app.aws import ec2 as ec2_module

    launch_result = ec2_module.launch_app_instance(
        session,
        deployment_id="dep000000001",
        repo_url="https://github.com/kaushik/agent",
        entry_point="compose",
        instance_type="t3.small",
        ebs_size_gb=20,
        secrets={},
    )

    response = client.post(
        "/terminate", json={"instance_id": launch_result["instance_id"], "region": settings.aws_default_region}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "terminating"}

    instance = session.client("ec2").describe_instances(InstanceIds=[launch_result["instance_id"]])[
        "Reservations"
    ][0]["Instances"][0]
    assert instance["State"]["Name"] in ("shutting-down", "terminated")


def test_terminate_returns_401_when_no_credentials_cached() -> None:
    response = client.post("/terminate", json={"instance_id": "i-0123456789abcdef0", "region": "us-east-1"})
    assert response.status_code == 401
    assert "No AWS credentials cached" in response.json()["detail"]


@patch(
    "app.api.deploy.terminate_instance",
    side_effect=ClientError(
        {
            "Error": {
                "Code": "UnrecognizedClientException",
                "Message": "The security token included in the request is invalid.",
            }
        },
        "TerminateInstances",
    ),
)
@patch("app.api.deploy.get_session")
def test_terminate_returns_401_with_cors_headers_when_aws_rejects_credentials(
    _mock_get_session, _mock_terminate
) -> None:
    response = client.post(
        "/terminate",
        json={"instance_id": "i-0123456789abcdef0", "region": "us-east-1"},
        headers=CORS_ORIGIN_HEADERS,
    )
    assert response.status_code == 401
    assert "security token" in response.json()["detail"]
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


@patch(
    "app.api.deploy.terminate_instance",
    side_effect=ClientError(
        {"Error": {"Code": "InvalidInstanceID.NotFound", "Message": "The instance ID does not exist"}},
        "TerminateInstances",
    ),
)
@patch("app.api.deploy.get_session")
def test_terminate_does_not_blame_credentials_for_unrelated_aws_errors(_mock_get_session, _mock_terminate) -> None:
    response = client.post(
        "/terminate",
        json={"instance_id": "i-doesnotexist", "region": "us-east-1"},
        headers=CORS_ORIGIN_HEADERS,
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "credentials" not in detail.lower()
    assert "does not exist" in detail
    assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_terminate_rejects_missing_instance_id() -> None:
    response = client.post("/terminate", json={"region": "us-east-1"})
    assert response.status_code == 422


def test_terminate_rejects_invalid_region() -> None:
    response = client.post("/terminate", json={"instance_id": "i-0123456789abcdef0", "region": "mars-1"})
    assert response.status_code == 422


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_without_pasted_keys_uses_the_backends_aws_credentials(_mock_validate, _mock_health) -> None:
    # moto puts fake AWS_* env vars in place — i.e. the credential chain has
    # credentials, exactly as an AWS profile would provide them.
    _bootstrap_moto_account()
    payload = {**EXTERNAL_LLM_PAYLOAD, "aws": {"region": settings.aws_default_region}}
    _response, status = _deploy_and_wait(payload)
    assert status["error"] is None
    assert status["result"]["app_instance_id"].startswith("i-")
