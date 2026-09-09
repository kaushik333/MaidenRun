from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.validation.compatibility import compatibility_checklist, render_checklist

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "fixtures"

client = TestClient(app)


def _texts(checks: list) -> list[tuple[bool, str]]:
    return [(c.ok, c.text) for c in checks]


def test_checklist_valid_compose() -> None:
    checks = compatibility_checklist(FIXTURES_DIR / "valid")
    assert _texts(checks) == [
        (True, "Single docker-compose.yml found at repo root"),
        (True, "A service maps to host port 8001 (agent UI exposed)"),
        (True, "No service maps to reserved ports 8002/8003"),
        (True, "No multi-file compose chaining detected"),
    ]


def test_checklist_port_8002_clash() -> None:
    checks = compatibility_checklist(FIXTURES_DIR / "port-8002-clash")
    assert _texts(checks) == [
        (True, "Single docker-compose.yml found at repo root"),
        (True, "A service maps to host port 8001 (agent UI exposed)"),
        (False, "A service maps to host port 8002 — conflicts with reserved feedback port"),
        (True, "No multi-file compose chaining detected"),
    ]


def test_checklist_missing_8001() -> None:
    checks = compatibility_checklist(FIXTURES_DIR / "missing-8001")
    assert _texts(checks) == [
        (True, "Single docker-compose.yml found at repo root"),
        (False, "No service maps to host port 8001 (agent UI not exposed)"),
        (True, "No service maps to reserved ports 8002/8003"),
        (True, "No multi-file compose chaining detected"),
    ]


def test_checklist_multi_file_compose() -> None:
    checks = compatibility_checklist(FIXTURES_DIR / "multi-file-compose")
    assert _texts(checks) == [
        (
            False,
            "Single docker-compose.yml found at repo root "
            "(found 2: docker-compose.override.yml, docker-compose.yml)",
        ),
        (False, "Could not confirm host port 8001 — resolve the entry point issue above first"),
        (False, "Could not confirm reserved ports 8002/8003 — resolve the entry point issue above first"),
        (False, "Multi-file compose setup detected — not supported in v1"),
    ]


def test_checklist_dockerfile_only() -> None:
    checks = compatibility_checklist(FIXTURES_DIR / "dockerfile-only-valid")
    assert _texts(checks) == [
        (True, "Single Dockerfile found at repo root"),
        (True, "Host port 8001 will be bound by the platform (Dockerfile-only entry point)"),
        (True, "No reserved-port conflict possible (Dockerfile-only entry point)"),
        (True, "No multi-file compose chaining detected"),
    ]


def test_render_checklist_uses_check_and_cross_marks() -> None:
    checks = compatibility_checklist(FIXTURES_DIR / "port-8002-clash")
    rendered = render_checklist(checks)
    assert rendered == (
        "✔ Single docker-compose.yml found at repo root\n"
        "✔ A service maps to host port 8001 (agent UI exposed)\n"
        "✘ A service maps to host port 8002 — conflicts with reserved feedback port\n"
        "✔ No multi-file compose chaining detected"
    )


def test_compatibility_report_endpoint_reports_clone_failure() -> None:
    response = client.get("/compatibility-report", params={"repo": "/nonexistent/definitely-not-a-repo"})
    assert response.status_code == 200
    body = response.json()
    assert len(body["checks"]) == 1
    assert body["checks"][0]["ok"] is False
    assert "Could not clone" in body["checks"][0]["text"]
    assert body["rendered"].startswith("✘ Could not clone")


def test_compatibility_report_endpoint_rejects_missing_repo_param() -> None:
    response = client.get("/compatibility-report")
    assert response.status_code == 422
