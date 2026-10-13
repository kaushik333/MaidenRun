import time
from unittest.mock import patch

import boto3
from fastapi.testclient import TestClient
from moto import mock_aws

from app.api import deploy as deploy_api
from app.aws import ec2
from app.aws.ec2 import HealthCheckTimeoutError
from app.core import history
from app.core.config import settings
from app.main import app
from app.validation.models import ValidateRepoResponse
from tests.conftest import create_instance_profile
from tests.test_deploy import EXTERNAL_LLM_PAYLOAD, _deploy_and_wait, _no_op_health

client = TestClient(app)
OLD_SHA, NEW_SHA = "1" * 40, "2" * 40


def _validated(sha: str, message: str) -> ValidateRepoResponse:
    return ValidateRepoResponse(valid=True, entry_point="compose", commit_sha=sha, commit_message=message)


def _ec2():
    return boto3.Session(region_name=settings.aws_default_region).client("ec2")


def _instance(instance_id: str) -> dict:
    return _ec2().describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]


def _address(allocation_id: str) -> dict:
    return _ec2().describe_addresses(AllocationIds=[allocation_id])["Addresses"][0]


def _poll(status_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"/deploy/{status_id}/status").json()
        if status.get("result") or status.get("error"):
            return status
        time.sleep(0.05)
    raise TimeoutError(status_id)


def _deploy_agent() -> dict:
    create_instance_profile(boto3.Session(region_name=settings.aws_default_region))
    with patch("app.api.deploy.validate_repo", return_value=_validated(OLD_SHA, "first")):
        response, status = _deploy_and_wait(EXTERNAL_LLM_PAYLOAD)
    assert status["error"] is None, status["error"]
    return history.get_deployment(response.json()["deployment_id"])


def _redeploy(deployment_id: str, sha: str = NEW_SHA, message: str = "fix citations", **json):
    with patch("app.api.deploy.validate_repo", return_value=_validated(sha, message)):
        return client.post(f"/deployments/{deployment_id}/redeploy", json=json)


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
def test_a_new_agent_gets_a_fixed_address_from_the_start(_mock_health) -> None:
    record = _deploy_agent()
    address = _address(record["allocation_id"])
    assert address["InstanceId"] == record["instance_id"]
    assert record["links"][0]["url"] == f"http://{address['PublicIp']}:{settings.app_port}"


@mock_aws
@patch("app.api.deploy.announce_agent_version")
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
def test_redeploy_moves_the_same_url_to_the_new_commit(_mock_health, mock_announce) -> None:
    before = _deploy_agent()

    response = _redeploy(before["deployment_id"])
    assert response.status_code == 202
    status = _poll(response.json()["deployment_id"])
    assert status["error"] is None
    assert status["steps"] == {"app_compute": "done", "app_health": "done", "switch": "done"}

    after = history.get_deployment(before["deployment_id"])
    assert after["links"] == before["links"]  # same URL
    assert after["allocation_id"] == before["allocation_id"]
    assert after["instance_id"] != before["instance_id"]
    assert _address(after["allocation_id"])["InstanceId"] == after["instance_id"]
    assert _instance(before["instance_id"])["State"]["Name"] in ("shutting-down", "terminated")
    assert after["security_group_id"] == before["security_group_id"]  # the allowlist carries over
    assert [g["GroupId"] for g in _instance(after["instance_id"])["SecurityGroups"]] == [after["security_group_id"]]
    assert (after["decisions"]["commit_sha"], after["decisions"]["commit_message"]) == (NEW_SHA, "fix citations")
    assert after["revisions"][0]["commit_sha"] == OLD_SHA
    mock_announce.assert_called_with(EXTERNAL_LLM_PAYLOAD["repo_url"], NEW_SHA, "fix citations")

    # The new instance reads the same stored secrets — nothing was rewritten.
    user_data = _ec2().describe_instance_attribute(InstanceId=after["instance_id"], Attribute="userData")
    import base64

    script = base64.b64decode(user_data["UserData"]["Value"]).decode()
    assert "OPENAI_API_KEY" in script and "PINECONE_API_KEY" in script
    assert f"checkout --detach {NEW_SHA}" in script


@mock_aws
def test_a_failed_redeploy_leaves_the_old_version_serving() -> None:
    with patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health):
        before = _deploy_agent()

    with patch("app.api.deploy.wait_for_port_health", side_effect=HealthCheckTimeoutError("never answered")):
        response = _redeploy(before["deployment_id"])
        status = _poll(response.json()["deployment_id"])

    assert "previous version is still serving" in status["error"]
    assert status["steps"]["app_health"] == "error" and status["steps"]["switch"] == "pending"
    after = history.get_deployment(before["deployment_id"])
    # Untouched apart from the failed attempt's own instance time, which still counts towards the cost.
    assert {k: v for k, v in after.items() if k != "extra_runs"} == before
    assert [run["outcome"] for run in after["extra_runs"]] == ["failed"]
    assert _instance(before["instance_id"])["State"]["Name"] == "running"
    assert _address(before["allocation_id"])["InstanceId"] == before["instance_id"]
    new_instances = [
        i for r in _ec2().describe_instances()["Reservations"] for i in r["Instances"]
        if i["InstanceId"] != before["instance_id"]
    ]
    assert all(i["State"]["Name"] in ("shutting-down", "terminated") for i in new_instances)


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
def test_an_agent_from_before_fixed_addresses_gets_one_on_its_first_redeploy(_mock_health) -> None:
    before = _deploy_agent()
    # Make it look like an agent deployed before fixed addresses existed.
    ec2.release_address(boto3.Session(region_name=settings.aws_default_region), before["allocation_id"])
    history.record_deployment({k: v for k, v in before.items() if k != "allocation_id"})

    status = _poll(_redeploy(before["deployment_id"]).json()["deployment_id"])
    assert status["error"] is None
    after = history.get_deployment(before["deployment_id"])
    assert after["allocation_id"]  # fixed from here on
    address = _address(after["allocation_id"])
    assert address["InstanceId"] == after["instance_id"]
    assert after["links"][0]["url"] == f"http://{address['PublicIp']}:{settings.app_port}"


@mock_aws
def test_redeploy_guards() -> None:
    assert client.post("/deployments/nope/redeploy", json={}).status_code == 404
    history.record_deployment({
        "deployment_id": "gone00000001", "kind": "agent", "status": "terminated", "deployed_at": "2026-10-13",
        "region": "us-east-1", "instance_id": "i-0", "links": [], "decisions": {"repo_url": "https://github.com/x/y"},
    })
    assert client.post("/deployments/gone00000001/redeploy", json={}).status_code == 409

    with patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health):
        record = _deploy_agent()
    deploy_api._redeploying.add(record["deployment_id"])
    try:
        response = _redeploy(record["deployment_id"])
        assert response.status_code == 409
        assert "already in progress" in response.json()["detail"]
    finally:
        deploy_api._redeploying.discard(record["deployment_id"])


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
def test_terminate_releases_the_address_and_follows_the_current_instance(_mock_health) -> None:
    before = _deploy_agent()
    _poll(_redeploy(before["deployment_id"]).json()["deployment_id"])
    current = history.get_deployment(before["deployment_id"])

    # A page opened before the redeploy still holds the old instance id.
    response = client.post("/terminate", json={
        "instance_id": before["instance_id"], "region": before["region"], "deployment_id": before["deployment_id"],
    })
    assert response.status_code == 200
    assert _instance(current["instance_id"])["State"]["Name"] in ("shutting-down", "terminated")
    assert _ec2().describe_addresses()["Addresses"] == []  # released, so it stops billing


@mock_aws
def test_sweep_lists_an_unattached_fixed_address_as_billing() -> None:
    session = boto3.Session(region_name=settings.aws_default_region)
    allocation_id, public_ip = ec2.allocate_address(session, "lost00000009")

    items = client.get("/leftovers").json()["items"]
    address = next(i for i in items if i["kind"] == "address")
    assert (address["id"], address["hourly_usd"]) == (allocation_id, 0.005)
    assert public_ip in address["label"]

    result = client.post("/leftovers/delete", json={"items": [address]}).json()
    assert result[0]["ok"] is True
    assert _ec2().describe_addresses()["Addresses"] == []


@mock_aws
def test_secrets_can_be_overwritten_for_a_rotated_token() -> None:
    session = boto3.Session(region_name=settings.aws_default_region)
    ec2.write_secret_parameters(session, "dep1", {"__repo_token": "old"})
    ec2.write_secret_parameters(session, "dep1", {"__repo_token": "new"})
    value = session.client("ssm").get_parameter(Name="/maidenrun/dep1/__repo_token", WithDecryption=True)
    assert value["Parameter"]["Value"] == "new"


def test_terminate_waits_for_an_in_flight_redeploy() -> None:
    deploy_api._redeploying.add("busy00000001")
    try:
        response = client.post(
            "/terminate", json={"instance_id": "i-0", "region": "us-east-1", "deployment_id": "busy00000001"}
        )
        assert response.status_code == 409
        assert "redeploy of this agent is in progress" in response.json()["detail"]
    finally:
        deploy_api._redeploying.discard("busy00000001")


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
def test_cost_keeps_accumulating_across_redeploys(_mock_health) -> None:
    before = _deploy_agent()
    _poll(_redeploy(before["deployment_id"]).json()["deployment_id"])
    after = history.get_deployment(before["deployment_id"])

    assert after["deployed_at"] == before["deployed_at"]  # the total never restarts
    assert [run["outcome"] for run in after["extra_runs"]] == ["redeployed"]  # the overlap is counted
    shown_in_history = next(
        r for r in client.get("/deployments").json() if r["deployment_id"] == before["deployment_id"]
    )["cost_estimate_usd"]
    shown_in_cost_monitor = client.get(f"/deploy/{before['deployment_id']}/cost-estimate").json()["estimated_usd"]
    assert abs(shown_in_history - shown_in_cost_monitor) < 1e-6  # both views agree
