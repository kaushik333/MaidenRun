from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str | bool]:
    # secret_required tells the frontend whether to offer the API secret field.
    return {"status": "ok", "secret_required": bool(settings.control_plane_secret)}
