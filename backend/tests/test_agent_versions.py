import io
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.aws import ec2
from app.core import history
from app.core.config import settings
from app.core.feedback_client import export_feedback, same_repo
from app.main import app
from app.validation.models import ValidateRepoResponse
from app.validation.validator import validate_repo
from tests.conftest import create_instance_profile
from tests.test_deploy import EXTERNAL_LLM_PAYLOAD, _deploy_and_wait, _no_op_health

client = TestClient(app)
SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = EXTERNAL_LLM_PAYLOAD["repo_url"]


class FakeFeedbackStack:
    """Stands in for a running feedback stack's dashboard: records every call
    and answers like the real admin/export API does."""

    def __init__(self, fail_with: int | None = None, xlsx_supported: bool = True):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.fail_with = fail_with
        self.xlsx_supported = xlsx_supported

    def __call__(self, request, timeout=None):
        parts = urlsplit(request.full_url)
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = json.loads(request.data) if request.data else None
        self.calls.append((request.get_method(), parts.path, params, body))
        if self.fail_with:
            error_body = io.BytesIO(b'{"detail": "x"}')
            raise urllib.error.HTTPError(request.full_url, self.fail_with, "error", {}, error_body)
        if parts.path == "/api/dashboard/export":
            if params["format"] == "xlsx" and not self.xlsx_supported:
                raise urllib.error.HTTPError(request.full_url, 422, "Unprocessable", {}, io.BytesIO(b"{}"))
            return io.BytesIO(f"{params['format']} bytes".encode())
        if parts.path == "/api/admin/questionnaires" and request.get_method() == "GET":
            question = {"question": "Q?", "answer": "", "options": ["Yes", "No"], "optional": False}
            questionnaire = {"version": 1, "is_active": True, "responses": 3, "questions": [question]}
            return io.BytesIO(json.dumps({"questionnaires": [questionnaire], "agent_versions": []}).encode())
        return io.BytesIO(b'{"version": 2, "id": 1}')


def _feedback_record(deployment_id: str = "fdbk00000001", agent_repo: str | None = REPO, status: str = "running"):
    record = {
        "deployment_id": deployment_id, "kind": "feedback", "status": status, "deployed_at": "2026-10-12T10:00:00",
        "region": "us-east-1", "instance_id": "i-0aaa", "links": [
            {"label": "Feedback form", "url": "http://203.0.113.5:8002"},
            {"label": "Feedback dashboard", "url": "http://203.0.113.5:8003/?token=tok"},
        ],
        "decisions": {"agent_repo": agent_repo},
    }
    history.record_deployment(record)
    return record


def test_validator_records_the_exact_commit_it_checked(tmp_path: Path) -> None:
    repo = tmp_path / "agent"
    repo.mkdir()
    (repo / "compose.yaml").write_text("services:\n  app:\n    build: .\n    ports: ['8001:8001']\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fix citations"], check=True)
    head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    result = validate_repo(str(repo))
    assert result.valid
    assert (result.commit_sha, result.commit_message) == (head, "fix citations")


def test_boot_script_runs_exactly_the_validated_commit() -> None:
    user_data = ec2.render_user_data(
        repo_url=REPO, entry_point="compose", secret_names=[], deployment_id="d1", region="us-east-1", commit_sha=SHA
    )
    assert f"git -C /opt/agent-repo fetch --depth 1 origin {SHA}" in user_data
    assert f"git -C /opt/agent-repo checkout --detach {SHA}" in user_data
    assert user_data.index("git clone") < user_data.index("checkout --detach") < user_data.index("docker compose")
    with pytest.raises(ValueError):
        ec2.render_user_data(
            repo_url=REPO, entry_point="compose", secret_names=[], deployment_id="d1", region="us-east-1",
            commit_sha="main; rm -rf /",
        )


@mock_aws
@patch("app.api.deploy.wait_for_port_health", side_effect=_no_op_health)
@patch(
    "app.api.deploy.validate_repo",
    return_value=ValidateRepoResponse(
        valid=True, entry_point="compose", commit_sha=SHA, commit_message="fix citations"
    ),
)
def test_agent_deploy_records_its_commit_and_tells_linked_feedback_stacks(*_mocks) -> None:
    create_instance_profile(boto3.Session(region_name=settings.aws_default_region))
    linked = _feedback_record("fdbk00000001", agent_repo=REPO + ".git")  # same repo, different spelling
    _feedback_record("fdbk00000002", agent_repo="https://github.com/someone/else")
    _feedback_record("fdbk00000003", agent_repo=REPO, status="terminated")
    stack = FakeFeedbackStack()

    with patch("app.core.feedback_client.urllib.request.urlopen", side_effect=stack):
        response, status = _deploy_and_wait(EXTERNAL_LLM_PAYLOAD)
    assert status["error"] is None

    decisions = history.get_deployment(response.json()["deployment_id"])["decisions"]
    assert (decisions["commit_sha"], decisions["commit_message"]) == (SHA, "fix citations")
    # Only the running stack linked to this repo was told — exactly once.
    assert stack.calls == [
        ("POST", "/api/admin/agent-version", {"token": "tok"},
         {"commit_sha": SHA, "commit_message": "fix citations", "repo_url": REPO}),
    ]
    assert linked["deployment_id"] == "fdbk00000001"


def test_a_new_feedback_stack_learns_which_agent_commit_is_live() -> None:
    history.record_deployment({
        "deployment_id": "agent0000001", "kind": "agent", "status": "running", "deployed_at": "2026-10-12T09:00:00",
        "region": "us-east-1", "instance_id": "i-0bbb", "links": [],
        "decisions": {"repo_url": REPO, "commit_sha": SHA, "commit_message": "initial"},
    })
    from app.core.feedback_client import link_to_live_agent

    stack = FakeFeedbackStack()
    with patch("app.core.feedback_client.urllib.request.urlopen", side_effect=stack):
        link_to_live_agent(_feedback_record())
    assert stack.calls[0][1] == "/api/admin/agent-version"
    assert stack.calls[0][3]["commit_sha"] == SHA


def test_questionnaire_endpoints_forward_to_the_running_stack() -> None:
    _feedback_record()
    stack = FakeFeedbackStack()
    with patch("app.core.feedback_client.urllib.request.urlopen", side_effect=stack):
        listed = client.get("/deployments/fdbk00000001/questionnaires")
        published = client.post(
            "/deployments/fdbk00000001/questionnaires",
            json={"questionnaire_json": '{"questions": []}', "activate": False},
        )
        activated = client.post("/deployments/fdbk00000001/questionnaires/1/activate")
        downloaded = client.get("/deployments/fdbk00000001/questionnaires/1/download")

    assert listed.json()["questionnaires"][0]["version"] == 1
    assert published.status_code == 201
    assert stack.calls[1] == (
        "POST",
        "/api/admin/questionnaires",
        {"token": "tok"},
        {"questionnaire_json": '{"questions": []}', "activate": False},
    )
    assert activated.status_code == 200 and stack.calls[2][1] == "/api/admin/questionnaires/1/activate"
    # Same format the deploy form takes, minus the per-response "answer" field.
    assert downloaded.json() == {"questions": [{"question": "Q?", "options": ["Yes", "No"], "optional": False}]}
    assert downloaded.headers["content-disposition"] == 'attachment; filename="questionnaire-v1.json"'


def test_an_older_stack_without_versioning_says_to_redeploy() -> None:
    _feedback_record()
    with patch("app.core.feedback_client.urllib.request.urlopen", side_effect=FakeFeedbackStack(fail_with=404)):
        response = client.get("/deployments/fdbk00000001/questionnaires")
    assert response.status_code == 404
    assert "redeploy" in response.json()["detail"]


def test_questionnaire_endpoints_refuse_terminated_or_unknown_stacks() -> None:
    _feedback_record(status="terminated")
    assert client.get("/deployments/fdbk00000001/questionnaires").status_code == 409
    assert client.get("/deployments/nope/questionnaires").status_code == 404


def test_export_falls_back_to_csv_for_a_stack_deployed_before_versioning() -> None:
    record = _feedback_record()
    with patch("app.core.feedback_client.urllib.request.urlopen", side_effect=FakeFeedbackStack(xlsx_supported=False)):
        paths = export_feedback(record)
    assert set(paths) == {"csv", "json"}
    assert Path(paths["csv"]).read_bytes() == b"csv bytes"


def test_same_repo_ignores_trailing_slash_dot_git_and_case() -> None:
    assert same_repo("https://github.com/X/Agent", "https://github.com/x/agent.git/")
    assert not same_repo("https://github.com/x/agent", "https://github.com/x/agent2")
    assert not same_repo(None, "https://github.com/x/agent")
