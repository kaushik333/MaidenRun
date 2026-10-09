from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.dashboard import router as dashboard_router
from app.api.feedback import router as feedback_router
from app.core.config import settings
from app.core.logging import logger
from app.db import ensure_schema, get_connection


@asynccontextmanager
async def lifespan(app: FastAPI):
    with get_connection() as conn:
        ensure_schema(conn)
    logger.info(
        "feedback-stack backend (%s) started for deployment %s", settings.service_role, settings.deployment_id
    )
    yield


app = FastAPI(title="Maidenrun — Feedback Stack", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Same image runs as two containers (see feedback-stack/docker-compose.yml)
# — the questionnaire form and the dashboard both want `/`, so which one a
# given process actually serves is decided by SERVICE_ROLE, not by code path.
if settings.service_role == "dashboard":
    app.include_router(dashboard_router)
else:
    app.include_router(feedback_router)
    # The built React questionnaire (frontend/dist, copied in by the
    # Dockerfile) — mounted last so it never shadows /api/* above. Absent in
    # local dev when running `uvicorn` directly against source instead of
    # the built image.
    _static_dir = Path(__file__).resolve().parent.parent / "static"
    if _static_dir.is_dir():
        app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")
