import hashlib
import hmac
import time

from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

client = TestClient(app)


def _sign(method: str, path: str, timestamp: str, body: bytes, secret: str) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}{path}{timestamp}{body_hash}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def test_no_secret_configured_allows_unsigned_requests() -> None:
    """The local/dev default — matches every other test in this suite."""
    assert settings.control_plane_secret == ""
    response = client.get("/deployments")
    assert response.status_code == 200


def test_missing_signature_rejected_when_secret_configured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "control_plane_secret", "test-secret")
    response = client.get("/deployments")
    assert response.status_code == 401


def test_valid_signature_accepted(monkeypatch) -> None:
    monkeypatch.setattr(settings, "control_plane_secret", "test-secret")
    timestamp = str(int(time.time()))
    signature = _sign("GET", "/deployments", timestamp, b"", "test-secret")
    response = client.get(
        "/deployments", headers={"X-Timestamp": timestamp, "X-Signature": signature}
    )
    assert response.status_code == 200


def test_wrong_secret_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "control_plane_secret", "test-secret")
    timestamp = str(int(time.time()))
    signature = _sign("GET", "/deployments", timestamp, b"", "wrong-secret")
    response = client.get(
        "/deployments", headers={"X-Timestamp": timestamp, "X-Signature": signature}
    )
    assert response.status_code == 401


def test_stale_timestamp_rejected(monkeypatch) -> None:
    monkeypatch.setattr(settings, "control_plane_secret", "test-secret")
    timestamp = str(int(time.time()) - 120)
    signature = _sign("GET", "/deployments", timestamp, b"", "test-secret")
    response = client.get(
        "/deployments", headers={"X-Timestamp": timestamp, "X-Signature": signature}
    )
    assert response.status_code == 401


def test_health_endpoint_never_requires_signature(monkeypatch) -> None:
    monkeypatch.setattr(settings, "control_plane_secret", "test-secret")
    response = client.get("/health")
    assert response.status_code == 200
