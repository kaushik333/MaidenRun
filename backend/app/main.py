from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.compatibility_report import router as compatibility_report_router
from app.api.deploy import rehydrate_agent_deployments
from app.api.deploy import router as deploy_router
from app.api.deploy_feedback import rehydrate_feedback_deployments
from app.api.deploy_feedback import router as deploy_feedback_router
from app.api.deployments import router as deployments_router
from app.api.health import router as health_router
from app.api.setup_info import router as setup_info_router
from app.api.validate_repo import router as validate_repo_router
from app.core.auth import require_signed_request
from app.core.config import settings
from app.core.logging import logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    rehydrate_agent_deployments()
    rehydrate_feedback_deployments()
    logger.info("backend started on port %s", settings.backend_port)
    yield


app = FastAPI(title="Maidenrun", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

signed = [Depends(require_signed_request)]

# /health is deliberately exempt — used for liveness probes/uptime checks
# that have no way to hold the control-plane secret.
app.include_router(health_router)
app.include_router(deploy_router, dependencies=signed)
app.include_router(deploy_feedback_router, dependencies=signed)
app.include_router(deployments_router, dependencies=signed)
app.include_router(validate_repo_router, dependencies=signed)
app.include_router(compatibility_report_router, dependencies=signed)
app.include_router(setup_info_router, dependencies=signed)
