from unittest.mock import patch

import boto3
from fastapi.testclient import TestClient
from moto import mock_aws

from app.core import history
from app.core.config import settings
from app.main import app
from tests.conftest import create_instance_profile
from tests.test_deploy import (
    EXTERNAL_LLM_PAYLOAD,
    VALID_COMPOSE_REPO,
    _deploy_and_wait,
    _no_op_health,
)

client = TestClient(app)

MY_IP = "192.0.2.50"
FEEDBACK_PAYLOAD = {
    "aws": {"access_key_id": "AKIA_TEST", "secret_access_key": "shh"},
    "questionnaire_json": '{"questions": []}',
}


def _session() -> boto3.Session:
    return boto3.Session(
        aws_access_key_id="test", aws_secret_access_key="test", region_name=settings.aws_default_region
    )


def _ingress(group_id: str) -> set[tuple[int, str]]:
    group = _session().client("ec2").describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    return {(p["FromPort"], r["CidrIp"]) for p in group["IpPermissions"] for r in p["IpRanges"]}


def _deploy_agent(allowed_ips: list[str]) -> dict:
    create_instance_profile(_session())
    response, status = _deploy_and_wait({**EXTERNAL_LLM_PAYLOAD, "allowed_ips": allowed_ips})
    assert status["error"] is None, status["error"]
    return history.get_deployment(response.json()["deployment_id"])


@mock_aws
@patch("app.aws.ec2.my_public_ip", return_value=MY_IP)
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_with_allowlist_admits_only_those_ips_plus_mine(*_mocks) -> None:
    record = _deploy_agent(["203.0.113.7", "198.51.100.0/24"])

    assert _ingress(record["security_group_id"]) == {
        (8001, "203.0.113.7/32"),
        (8001, "198.51.100.0/24"),
        (8001, f"{MY_IP}/32"),
    }
    # History keeps what the user typed (normalized), not the auto-added IP.
    assert record["decisions"]["allowed_ips"] == ["203.0.113.7/32", "198.51.100.0/24"]


@mock_aws
@patch("app.aws.ec2.my_public_ip", side_effect=AssertionError("must not look up the IP for an open deploy"))
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_without_allowlist_is_open_to_everyone(*_mocks) -> None:
    record = _deploy_agent([])
    assert _ingress(record["security_group_id"]) == {(8001, "0.0.0.0/0")}


@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_rejects_an_invalid_ip_before_touching_aws(_mock_validate) -> None:
    response = client.post("/deploy", json={**EXTERNAL_LLM_PAYLOAD, "allowed_ips": ["not-an-ip"]})
    assert response.status_code == 422
    assert "not-an-ip" in response.text


@patch("app.aws.ec2.my_public_ip", side_effect=OSError("network unreachable"))
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_deploy_reports_a_failed_ip_lookup_clearly(*_mocks) -> None:
    response = client.post("/deploy", json={**EXTERNAL_LLM_PAYLOAD, "allowed_ips": ["203.0.113.7"]})
    assert response.status_code == 502
    assert "public IP" in response.json()["detail"]


@mock_aws
@patch("app.aws.ec2.my_public_ip", return_value=MY_IP)
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_edit_allowed_ips_updates_rules_in_place_without_redeploying(*_mocks) -> None:
    record = _deploy_agent([])
    group_id, instance_id = record["security_group_id"], record["instance_id"]

    response = client.put(f"/deployments/{record['deployment_id']}/allowed-ips", json={"allowed_ips": ["203.0.113.9"]})
    assert response.status_code == 200, response.text
    assert response.json()["decisions"]["allowed_ips"] == ["203.0.113.9/32"]
    assert _ingress(group_id) == {(8001, "203.0.113.9/32"), (8001, f"{MY_IP}/32")}

    response = client.put(f"/deployments/{record['deployment_id']}/allowed-ips", json={"allowed_ips": []})
    assert response.status_code == 200
    assert _ingress(group_id) == {(8001, "0.0.0.0/0")}

    # Same instance throughout — nothing was relaunched.
    assert history.get_deployment(record["deployment_id"])["instance_id"] == instance_id


def test_edit_allowed_ips_returns_404_for_unknown_deployment() -> None:
    response = client.put("/deployments/nope/allowed-ips", json={"allowed_ips": []})
    assert response.status_code == 404


def test_edit_allowed_ips_refuses_deployments_without_their_own_security_group() -> None:
    history.record_deployment({
        "deployment_id": "old000000001",
        "kind": "agent",
        "status": "running",
        "deployed_at": "2026-10-01T00:00:00+00:00",
        "region": "us-east-1",
        "instance_id": "i-0123456789abcdef0",
        "links": [],
    })
    response = client.put("/deployments/old000000001/allowed-ips", json={"allowed_ips": ["203.0.113.7"]})
    assert response.status_code == 409
    assert "redeploy" in response.json()["detail"]


@mock_aws
@patch("app.aws.ec2.my_public_ip", return_value=MY_IP)
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_terminate_deletes_the_deployments_security_group(*_mocks) -> None:
    record = _deploy_agent(["203.0.113.7"])

    response = client.post(
        "/terminate",
        json={
            "instance_id": record["instance_id"],
            "region": record["region"],
            "deployment_id": record["deployment_id"],
        },
    )
    assert response.status_code == 200

    groups = _session().client("ec2").describe_security_groups()["SecurityGroups"]
    assert record["security_group_id"] not in {g["GroupId"] for g in groups}


@mock_aws
@patch("app.aws.ec2.my_public_ip", return_value=MY_IP)
@patch("app.api.deploy_feedback.wait_for_port_health", side_effect=_no_op_health)
def test_feedback_deploy_with_allowlist_covers_form_and_dashboard_ports(*_mocks) -> None:
    create_instance_profile(_session())
    response = client.post("/deploy-feedback", json={**FEEDBACK_PAYLOAD, "allowed_ips": ["203.0.113.7"]})
    assert response.status_code == 202, response.text
    deployment_id = response.json()["deployment_id"]
    assert client.get(f"/deploy-feedback/{deployment_id}/status").json()["error"] is None

    record = history.get_deployment(deployment_id)
    assert _ingress(record["security_group_id"]) == {
        (port, cidr) for port in (8002, 8003) for cidr in ("203.0.113.7/32", f"{MY_IP}/32")
    }


@patch("app.api.setup_info.my_public_ip", return_value=MY_IP)
def test_my_ip_endpoint(_mock) -> None:
    response = client.get("/my-ip")
    assert response.status_code == 200
    assert response.json() == {"ip": MY_IP}
