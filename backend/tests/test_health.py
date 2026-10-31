from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "secret_required": False}


def test_unexpected_errors_reach_the_browser_as_json_with_cors_headers(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.api import setup_info
    from app.main import app

    def boom():
        raise RuntimeError("something broke")

    monkeypatch.setattr(setup_info, "my_public_ip", boom)
    response = TestClient(app, raise_server_exceptions=False).get(
        "/my-ip", headers={"Origin": "http://localhost:5173"}
    )
    assert response.status_code == 500
    assert response.json() == {"detail": "Unexpected backend error: something broke"}
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
