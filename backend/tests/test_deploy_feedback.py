import json
import time
from unittest.mock import patch

import boto3
from fastapi.testclient import TestClient
from moto import mock_aws

from app.aws.credentials import get_session
from app.aws.ec2 import HealthCheckTimeoutError
from app.core.config import settings
from app.main import app
from tests.conftest import create_instance_profile

client = TestClient(app)

AWS = {"access_key_id": "AKIA_TEST", "secret_access_key": "shh"}
QUESTIONNAIRE = json.dumps({"questions": [{"question": "How was it?", "answer": ""}]})


def _bootstrap_moto_account() -> None:
    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    create_instance_profile(session)


def _no_op_health(*_args, **_kwargs) -> None:
    return None


def _poll_until_terminal(deployment_id: str, timeout_seconds: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = client.get(f"/deploy-feedback/{deployment_id}/status").json()
        if status.get("result") is not None or status.get("error") is not None:
            return status
        time.sleep(0.05)
    raise TimeoutError(f"feedback deployment {deployment_id} never reached a terminal state")


def _deploy_feedback_and_wait(payload: dict) -> tuple[object, dict]:
    response = client.post("/deploy-feedback", json=payload)
    assert response.status_code == 202, response.text
    status = _poll_until_terminal(response.json()["deployment_id"])
    return response, status


@mock_aws
@patch("app.api.deploy_feedback.wait_for_port_health", side_effect=_no_op_health)
def test_deploy_feedback_launches_and_returns_real_links(_mock_health) -> None:
    _bootstrap_moto_account()
    _response, status = _deploy_feedback_and_wait({"aws": AWS, "questionnaire_json": QUESTIONNAIRE})

    assert status["error"] is None
    result = status["result"]
    assert result["feedback_form_url"].startswith("http://")
    assert result["feedback_form_url"].endswith(":8002")
    assert ":8003" in result["feedback_dashboard_url"]
    assert "token=" in result["feedback_dashboard_url"]
    assert result["feedback_instance_id"].startswith("i-")
    assert all(step == "done" for step in status["steps"].values())


@mock_aws
@patch("app.api.deploy_feedback.wait_for_port_health", side_effect=_no_op_health)
def test_deploy_feedback_sets_the_given_instance_name(_mock_health) -> None:
    _bootstrap_moto_account()
    payload = {"aws": AWS, "instance_name": "my-feedback", "questionnaire_json": QUESTIONNAIRE}
    _response, status = _deploy_feedback_and_wait(payload)

    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    instance = session.client("ec2").describe_instances(InstanceIds=[status["result"]["feedback_instance_id"]])[
        "Reservations"
    ][0]["Instances"][0]
    name_tag = next(t["Value"] for t in instance["Tags"] if t["Key"] == "Name")
    assert name_tag == "my-feedback"


@mock_aws
@patch("app.api.deploy_feedback.wait_for_port_health", side_effect=_no_op_health)
def test_deploy_feedback_deletes_s3_bundle_after_successful_health_check(_mock_health) -> None:
    _bootstrap_moto_account()
    _response, status = _deploy_feedback_and_wait({"aws": AWS, "questionnaire_json": QUESTIONNAIRE})
    assert status["result"] is not None

    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    account_id = session.client("sts").get_caller_identity()["Account"]
    bucket = f"agent-feedback-deploy-{account_id}"
    objects = session.client("s3").list_objects_v2(Bucket=bucket).get("Contents", [])
    assert objects == []


def test_deploy_feedback_rejects_invalid_json_before_touching_aws() -> None:
    response = client.post("/deploy-feedback", json={"aws": AWS, "questionnaire_json": "not valid json"})
    assert response.status_code == 400
    assert "not valid JSON" in response.json()["detail"]


def test_deploy_feedback_rejects_empty_questionnaire() -> None:
    response = client.post("/deploy-feedback", json={"aws": AWS, "questionnaire_json": ""})
    assert response.status_code == 422


def test_deploy_feedback_returns_401_when_no_credentials_cached() -> None:
    response = client.post(
        "/deploy-feedback", json={"aws": {"region": "us-east-1"}, "questionnaire_json": QUESTIONNAIRE}
    )
    assert response.status_code == 401
    assert "No AWS credentials cached" in response.json()["detail"]


@mock_aws
def test_deploy_feedback_reuses_cached_credentials_when_omitted() -> None:
    _bootstrap_moto_account()
    get_session("AKIA_TEST", "shh", region=settings.aws_default_region)  # seed the cache

    with patch("app.api.deploy_feedback.wait_for_port_health", side_effect=_no_op_health):
        response = client.post(
            "/deploy-feedback",
            json={"aws": {"region": settings.aws_default_region}, "questionnaire_json": QUESTIONNAIRE},
        )
        assert response.status_code == 202
        status = _poll_until_terminal(response.json()["deployment_id"])

    assert status["result"] is not None


@mock_aws
def test_deploy_feedback_rolls_back_instance_and_bundle_on_health_check_failure() -> None:
    _bootstrap_moto_account()

    with patch(
        "app.api.deploy_feedback.wait_for_port_health",
        side_effect=HealthCheckTimeoutError("did not respond on port 8002 within 300s"),
    ):
        response = client.post("/deploy-feedback", json={"aws": AWS, "questionnaire_json": QUESTIONNAIRE})
        status = _poll_until_terminal(response.json()["deployment_id"])

    assert status["result"] is None
    assert "did not respond on port 8002" in status["error"]
    assert status["steps"]["feedback_health"] == "error"

    session = boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )
    instances = session.client("ec2").describe_instances()["Reservations"]
    states = [i["State"]["Name"] for r in instances for i in r["Instances"]]
    assert states == ["shutting-down"] or states == ["terminated"]

    account_id = session.client("sts").get_caller_identity()["Account"]
    bucket = f"agent-feedback-deploy-{account_id}"
    objects = session.client("s3").list_objects_v2(Bucket=bucket).get("Contents", [])
    assert objects == []


def test_deploy_feedback_status_returns_404_for_unknown_deployment_id() -> None:
    response = client.get("/deploy-feedback/does-not-exist/status")
    assert response.status_code == 404
