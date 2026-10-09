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
    assert "Multiple compose files" in result.error


def test_compose_yaml_repo_passes() -> None:
    result = validate_repo_dir(FIXTURES_DIR / "compose-yaml-valid")
    assert result.valid is True
    assert result.entry_point == "compose"


def test_compose_yaml_alongside_docker_compose_yml_is_rejected_as_multi_file(tmp_path: Path) -> None:
    compose = (FIXTURES_DIR / "valid" / "docker-compose.yml").read_text()
    (tmp_path / "compose.yaml").write_text(compose)
    (tmp_path / "docker-compose.yml").write_text(compose)
    result = validate_repo_dir(tmp_path)
    assert result.valid is False
    assert "compose.yaml, docker-compose.yml" in result.error


def test_compose_errors_name_the_actual_file(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services:\n  app:\n    ports: ['3000:3000']\n")
    result = validate_repo_dir(tmp_path)
    assert result.valid is False
    assert "update compose.yaml" in result.error


def test_no_entry_point_rejected(tmp_path: Path) -> None:
    result = validate_repo_dir(tmp_path)
    assert result.valid is False
    assert "No entry point found" in result.error


def test_clone_repo_reports_git_failure(tmp_path: Path) -> None:
    error = clone_repo("/nonexistent/definitely-not-a-repo", tmp_path / "repo")
    assert error is not None
    assert "Could not clone" in error


def test_clone_repo_with_token_uses_authenticated_url() -> None:
    with patch("app.validation.validator.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        mock_run.return_value.stderr = ""
        clone_repo("https://github.com/kaushik/agent", Path("/tmp/wherever"), github_token="ghp_secrettoken")

    cloned_url = mock_run.call_args.args[0][4]  # ["git", "clone", "--depth", "1", <url>, dest]
    assert cloned_url == "https://ghp_secrettoken@github.com/kaushik/agent"


def test_clone_repo_error_never_contains_raw_token(tmp_path: Path) -> None:
    with patch("app.validation.validator.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 128
        mock_run.return_value.stderr = "fatal: repository 'https://ghp_secrettoken@github.com/x/y' not found"
        error = clone_repo(
            "https://github.com/x/y", tmp_path / "repo", github_token="ghp_secrettoken"
        )

    assert "ghp_secrettoken" not in error
    assert "https://github.com/x/y" in error  # the original, unauthenticated URL is still reported
    assert "***" in error


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


def test_validate_repo_endpoint_passes_github_token_through() -> None:
    with patch("app.api.validate_repo.validate_repo") as mock_validate:
        mock_validate.return_value.valid = True
        mock_validate.return_value.entry_point = "compose"
        mock_validate.return_value.error = None
        client.post(
            "/validate-repo",
            json={"repo_url": "https://github.com/x/y", "github_token": "ghp_secrettoken"},
        )
    mock_validate.assert_called_once_with("https://github.com/x/y", "ghp_secrettoken")

