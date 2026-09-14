import hashlib
import hmac
import time

from fastapi import Header, HTTPException, Request

from app.core.config import settings

SIGNATURE_TTL_SECONDS = 60


def _expected_signature(method: str, path: str, timestamp: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    message = f"{method}{path}{timestamp}{body_hash}".encode()
    return hmac.new(settings.control_plane_secret.encode(), message, hashlib.sha256).hexdigest()


async def require_signed_request(
    request: Request,
    x_timestamp: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
) -> None:
    """Verifies every request was signed by whoever holds control_plane_secret.

    A no-op when no secret is configured — the local/dev default, so a bare
    `docker compose up` with no CONTROL_PLANE_SECRET set behaves exactly as
    it did before this existed. The secret itself never crosses the wire,
    only a per-request signature over it, so this holds up even without TLS
    in front of the backend.
    """
    if not settings.control_plane_secret:
        return

    if not x_timestamp or not x_signature:
        raise HTTPException(status_code=401, detail="Missing request signature")

    try:
        if abs(time.time() - float(x_timestamp)) > SIGNATURE_TTL_SECONDS:
            raise HTTPException(status_code=401, detail="Stale request signature")
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid timestamp") from None

    body = await request.body()
    expected = _expected_signature(request.method, request.url.path, x_timestamp, body)
    if not hmac.compare_digest(expected, x_signature):
        raise HTTPException(status_code=401, detail="Invalid request signature")
