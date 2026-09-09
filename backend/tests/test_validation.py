import subprocess
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.validation.validator import clone_repo, validate_repo_dir

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

client = TestClient(app)


def test_valid_compose_repo_passes() -> None:
    result = validate_repo_dir(FIXTURES_DIR / "valid")
    assert result.valid is True
    assert result.entry_point == "compose"
    assert result.error is None


def test_dockerfile_only_repo_passes() -> None:
    result = validate_repo_dir(FIXTURES_DIR / "dockerfile-only-valid")
    assert result.valid is True
    assert result.entry_point == "dockerfile"
    assert result.error is None


def test_port_8002_clash_rejected() -> None:
    result = validate_repo_dir(FIXTURES_DIR / "port-8002-clash")
    assert result.valid is False
    assert "8002" in result.error
    assert "reserved by the platform for feedback capture" in result.error


def test_missing_8001_rejected() -> None:
    result = validate_repo_dir(FIXTURES_DIR / "missing-8001")
    assert result.valid is False
    assert "8001" in result.error


def test_multi_file_compose_rejected() -> None:
    result = validate_repo_dir(FIXTURES_DIR / "multi-file-compose")
    assert result.valid is False
    assert "Multiple docker-compose files" in result.error


def test_no_entry_point_rejected(tmp_path: Path) -> None:
    result = validate_repo_dir(tmp_path)
    assert result.valid is False
    assert "No entry point found" in result.error


def test_clone_repo_reports_git_failure(tmp_path: Path) -> None:
    error = clone_repo("/nonexistent/definitely-not-a-repo", tmp_path / "repo")
    assert error is not None
    assert "Could not clone" in error


def test_clone_repo_reports_timeout_instead_of_crashing(tmp_path: Path) -> None:
    with patch(
        "app.validation.validator.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30),
    ):
        error = clone_repo("https://example.com/slow.git", tmp_path / "repo")
    assert error is not None
    assert "took too long" in error


def test_validate_repo_endpoint_reports_clone_failure() -> None:
    response = client.post("/validate-repo", json={"repo_url": "/nonexistent/definitely-not-a-repo"})
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert "Could not clone" in body["error"]


def test_validate_repo_endpoint_rejects_empty_repo_url() -> None:
    response = client.post("/validate-repo", json={"repo_url": ""})
    assert response.status_code == 422
