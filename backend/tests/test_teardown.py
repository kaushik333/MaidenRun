import io
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from moto import mock_aws

from app.aws import ec2
from app.aws.s3_bundle import ensure_bundle_bucket
from app.aws.sweep import scan_region
from app.core import history
from app.core.config import settings
from app.core.errors import classify_error
from app.main import app
from tests.conftest import create_instance_profile
from tests.test_deploy import EXTERNAL_LLM_PAYLOAD, VALID_COMPOSE_REPO, _deploy_and_wait, _no_op_health

client = TestClient(app)

FEEDBACK_PAYLOAD = {
    "aws": {"access_key_id": "AKIA_TEST", "secret_access_key": "shh"},
    "questionnaire_json": '{"questions": []}',
}
XLSX_EXPORT = b"PK\x03\x04 fake workbook bytes"
JSON_EXPORT = b'[{"id": 1, "answers": [{"question": "Did it help?", "answer": "Yes"}]}]'


def _session(region: str = settings.aws_default_region) -> boto3.Session:
    return boto3.Session(aws_access_key_id="test", aws_secret_access_key="test", region_name=region)


def _fake_dashboard(request, timeout=None):
    """Stands in for the running feedback stack's /api/dashboard/export."""
    return io.BytesIO(XLSX_EXPORT if "format=xlsx" in request.full_url else JSON_EXPORT)


def _secret_names(deployment_id: str) -> list[str]:
    params = _session().client("ssm").describe_parameters()["Parameters"]
    return [p["Name"] for p in params if p["Name"].startswith(f"/maidenrun/{deployment_id}/")]


def _instance_state(instance_id: str) -> str:
    reservations = _session().client("ec2").describe_instances(InstanceIds=[instance_id])["Reservations"]
    return reservations[0]["Instances"][0]["State"]["Name"]


def _deploy_feedback() -> dict:
    create_instance_profile(_session())
    with patch("app.api.deploy_feedback.wait_for_port_health", side_effect=_no_op_health):
        response = client.post("/deploy-feedback", json=FEEDBACK_PAYLOAD)
    assert response.status_code == 202, response.text
    return history.get_deployment(response.json()["deployment_id"])


def _terminate(record: dict, force: bool = False):
    return client.post(
        "/terminate",
        json={
            "instance_id": record["instance_id"],
            "region": record["region"],
            "deployment_id": record["deployment_id"],
            "force": force,
        },
    )


@mock_aws
@patch("app.core.feedback_client.urllib.request.urlopen", side_effect=_fake_dashboard)
def test_feedback_teardown_exports_first_then_deletes_everything(_mock_urlopen) -> None:
    record = _deploy_feedback()
    assert _secret_names(record["deployment_id"])  # POSTGRES_PASSWORD + DASHBOARD_TOKEN exist

    response = _terminate(record)
    assert response.status_code == 200, response.text
    exports = response.json()["exports"]
    assert Path(exports["xlsx"]).read_bytes() == XLSX_EXPORT
    assert Path(exports["json"]).read_bytes() == JSON_EXPORT

    assert _instance_state(record["instance_id"]) in ("shutting-down", "terminated")
    assert _secret_names(record["deployment_id"]) == []
    saved = history.get_deployment(record["deployment_id"])
    assert saved["status"] == "terminated" and saved["exports"] == exports

    download = client.get(f"/deployments/{record['deployment_id']}/export/xlsx")
    assert download.status_code == 200
    assert download.content == XLSX_EXPORT


@mock_aws
@patch("app.core.feedback_client.urllib.request.urlopen", side_effect=urllib.error.URLError("timed out"))
def test_feedback_teardown_stops_when_export_fails_and_deletes_nothing(_mock_urlopen) -> None:
    record = _deploy_feedback()

    response = _terminate(record)
    assert response.status_code == 409
    assert "Nothing was deleted" in response.json()["detail"]
    assert _instance_state(record["instance_id"]) == "running"
    assert _secret_names(record["deployment_id"])
    assert history.get_deployment(record["deployment_id"])["status"] == "running"


@mock_aws
@patch("app.core.feedback_client.urllib.request.urlopen", side_effect=urllib.error.URLError("timed out"))
def test_terminate_anyway_skips_the_export(_mock_urlopen) -> None:
    record = _deploy_feedback()

    response = _terminate(record, force=True)
    assert response.status_code == 200
    assert response.json()["exports"] is None
    assert _instance_state(record["instance_id"]) in ("shutting-down", "terminated")
    assert _secret_names(record["deployment_id"]) == []


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch("app.api.deploy.validate_repo", return_value=VALID_COMPOSE_REPO)
def test_agent_teardown_deletes_its_secrets_without_exporting(*_mocks) -> None:
    create_instance_profile(_session())
    response, status = _deploy_and_wait(EXTERNAL_LLM_PAYLOAD)
    record = history.get_deployment(response.json()["deployment_id"])
    assert set(_secret_names(record["deployment_id"])) == {
        f"/maidenrun/{record['deployment_id']}/OPENAI_API_KEY",
        f"/maidenrun/{record['deployment_id']}/PINECONE_API_KEY",
    }

    response = _terminate(record)
    assert response.status_code == 200
    assert response.json()["exports"] is None
    assert _secret_names(record["deployment_id"]) == []


def test_export_download_404s_without_a_saved_export() -> None:
    assert client.get("/deployments/nope/export/csv").status_code == 404


@mock_aws
def test_sweep_finds_only_what_belongs_to_no_running_deployment() -> None:
    session = _session()
    create_instance_profile(session)
    # A running deployment the control plane knows about — must NOT be listed.
    kept = ec2.launch_app_instance(
        session, deployment_id="kept00000001", repo_url="https://github.com/x/y", entry_point="compose",
        instance_type="t3.small", ebs_size_gb=20, secrets={"KEY": "v"},
    )
    history.record_deployment({
        "deployment_id": "kept00000001", "kind": "agent", "status": "running", "deployed_at": "2026-10-09",
        "region": settings.aws_default_region, "instance_id": kept["instance_id"], "links": [],
    })
    # An orphan the control plane lost track of, in a different region.
    orphan_session = _session("eu-west-1")  # IAM is global — the instance role above covers it
    orphan = ec2.launch_app_instance(
        orphan_session, deployment_id="lost00000001", repo_url="https://github.com/x/y", entry_point="compose",
        instance_type="t3.medium", ebs_size_gb=20, secrets={"KEY": "v"},
    )
    ensure_bundle_bucket(session)

    response = client.get("/leftovers")
    assert response.status_code == 200, response.text
    found = {(item["kind"], item["region"], item["id"]) for item in response.json()["items"]}

    assert ("instance", "eu-west-1", orphan["instance_id"]) in found
    assert ("secrets", "eu-west-1", "lost00000001") in found
    assert ("bucket", settings.aws_default_region, ensure_bundle_bucket(session)) in found
    assert not any(kept["instance_id"] == i or "kept00000001" == i for _, _, i in found)
    assert ("security_group", settings.aws_default_region, kept["security_group_id"]) not in found
    instance_item = next(i for i in response.json()["items"] if i["id"] == orphan["instance_id"])
    assert instance_item["hourly_usd"] > 0  # surfaced so the user sees it's costing money


@mock_aws
def test_deleting_leftovers_reports_per_item_results() -> None:
    session = _session()
    create_instance_profile(session)
    orphan = ec2.launch_app_instance(
        session, deployment_id="lost00000002", repo_url="https://github.com/x/y", entry_point="compose",
        instance_type="t3.small", ebs_size_gb=20, secrets={"KEY": "v"},
    )
    unused_group = ec2.create_security_group(session, "maidenrun-app-gone", "test", [8001], [ec2.OPEN_CIDR], "gone")
    region = settings.aws_default_region

    response = client.post("/leftovers/delete", json={"items": [
        {"kind": "instance", "region": region, "id": orphan["instance_id"]},
        {"kind": "secrets", "region": region, "id": "lost00000002"},
        {"kind": "security_group", "region": region, "id": unused_group},
        {"kind": "security_group", "region": region, "id": "sg-doesnotexist000"},
    ]})
    assert response.status_code == 200
    results = response.json()
    assert [r["ok"] for r in results] == [True, True, True, False]
    assert results[3]["error"]  # the failure is reported, the others still went through
    assert _instance_state(orphan["instance_id"]) in ("shutting-down", "terminated")
    assert _secret_names("lost00000002") == []


def test_missing_iam_permission_is_reported_as_an_outdated_policy_not_bad_credentials() -> None:
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "DescribeParameters"
    )
    status, detail = classify_error(denied)
    assert status == 403
    assert "infra/iam-policy.json" in detail
    assert "rejected these credentials" not in detail

    invalid_key = ClientError({"Error": {"Code": "InvalidClientTokenId", "Message": "bad key"}}, "GetCallerIdentity")
    assert classify_error(invalid_key) == (401, "AWS rejected these credentials: bad key")


@mock_aws
def test_sweep_still_lists_instances_when_secrets_cannot_be_checked(monkeypatch) -> None:
    session = _session()
    create_instance_profile(session)
    orphan = ec2.launch_app_instance(
        session, deployment_id="lost00000003", repo_url="https://github.com/x/y", entry_point="compose",
        instance_type="t3.small", ebs_size_gb=20, secrets={},
    )
    real_client = session.client

    def denied_pages(**_kwargs):
        # Like a real paginator: the AWS call (and so the error) happens on iteration.
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "DescribeParameters")
        yield

    def client(name, *args, **kwargs):
        made = real_client(name, *args, **kwargs)
        if name == "ssm":
            made.get_paginator = lambda _op: MagicMock(paginate=denied_pages)
        return made

    monkeypatch.setattr(session, "client", client)
    warnings: set[str] = set()
    items = scan_region(session, running_ids=set(), warnings=warnings)

    assert orphan["instance_id"] in {item["id"] for item in items}
    assert len(warnings) == 1 and "ssm:DescribeParameters" in next(iter(warnings))


@mock_aws
def test_a_missing_permission_on_delete_says_how_to_fix_it(monkeypatch) -> None:
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform: s3:DeleteBucket"}}, "DeleteBucket"
    )
    monkeypatch.setattr("app.api.deployments.delete_leftover", MagicMock(side_effect=denied))

    response = client.post(
        "/leftovers/delete", json={"items": [{"kind": "bucket", "region": "us-east-1", "id": "maidenrun-123"}]}
    )
    result = response.json()[0]
    assert result["ok"] is False
    assert "infra/iam-policy.json" in result["error"]
    assert "s3:DeleteBucket" in result["error"]  # AWS's own detail is kept
