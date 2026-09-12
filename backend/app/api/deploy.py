import threading
import uuid
from datetime import datetime, timezone
from typing import Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.aws.credentials import NoCredentialsError, get_session
from app.aws.ec2 import launch_app_instance, terminate_instance, wait_for_port_health
from app.aws.pricing import EC2_HOURLY_RATES, estimate_ec2_cost
from app.core import history
from app.core.config import settings
from app.core.errors import classify_error, reraise_as_http_error
from app.core.logging import logger
from app.validation.validator import validate_repo

CPU_INSTANCE_TYPES = ("t3.small", "t3.medium", "m5.xlarge")
# Curated, not exhaustive — same "no confusing options" reasoning as the
# instance-type/EBS-size dropdowns. IAM/the instance role are global and
# work in any of these; only the default-VPC requirement is per-region.
AWS_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "eu-west-1",
    "eu-central-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ap-northeast-1",
    "me-central-1",
)

router = APIRouter()


class AwsCredentials(BaseModel):
    access_key_id: str = Field(min_length=1)
    secret_access_key: str = Field(min_length=1)
    region: Literal[
        "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1",
        "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "me-central-1",
    ] = "us-east-1"


class EnvVar(BaseModel):
    key: str = Field(min_length=1)
    value: str


class DeployRequest(BaseModel):
    repo_url: str = Field(min_length=1)
    aws: AwsCredentials
    instance_type: str
    ebs_size_gb: Literal[20, 50, 100]
    instance_name: str | None = None
    # No provider concept at all — the agent connects to whatever LLM
    # endpoint it wants (a real provider's API, or one the user hosts
    # themselves elsewhere; provisioning compute for the LLM itself is out
    # of scope here). Both pairs are optional: some self-hosted endpoints
    # need no key, some agents need no override of their default base URL.
    api_key_name: str | None = None
    api_key_value: str | None = None
    base_url_name: str | None = None
    base_url_value: str | None = None
    env_vars: list[EnvVar] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_fields(self) -> "DeployRequest":
        if self.instance_type not in CPU_INSTANCE_TYPES:
            raise ValueError(f"instance_type must be one of {CPU_INSTANCE_TYPES}")
        if bool(self.api_key_name) != bool(self.api_key_value):
            raise ValueError("api_key_name and api_key_value must be provided together")
        if bool(self.base_url_name) != bool(self.base_url_value):
            raise ValueError("base_url_name and base_url_value must be provided together")
        return self


class DeployResult(BaseModel):
    """What a deployment produces once every step has actually succeeded —
    the "result" field of DeploymentStatus once polling reaches "done"."""

    app_url: str
    app_instance_id: str
    instance_type: str
    ebs_size_gb: int
    launched_at: datetime


class DeployAcceptedResponse(BaseModel):
    deployment_id: str


class CostEstimate(BaseModel):
    estimated_usd: float
    hourly_rate: float
    instance_type: str
    launched_at: datetime


StepStatus = Literal["pending", "active", "done", "error"]
DEPLOY_STEP_IDS = ("app_compute", "app_health")


class DeploymentStatus(BaseModel):
    steps: dict[str, StepStatus]
    result: DeployResult | None = None
    error: str | None = None


class TerminateRequest(BaseModel):
    instance_id: str = Field(min_length=1)
    region: Literal[
        "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1",
        "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "me-central-1",
    ] = "us-east-1"
    # Present when the caller is terminating from a known deployment (a
    # fresh CostMonitor or the Deployments history tab) — lets this endpoint
    # flip that deployment's history record to "terminated". Absent for any
    # older/other caller that only knows the instance; the instance still
    # terminates either way, it just won't be reflected in history.
    deployment_id: str | None = None


class TerminateResponse(BaseModel):
    status: str


# ---- deployment status store ----
# In-memory, single dict for the whole process — this is a local, single-user
# control plane, not a distributed system, so a database or external store
# would be real infrastructure weight for no real benefit. Guarded by a lock
# since the background task thread (writer) and each polling request's own
# thread (reader) touch it concurrently. Grows for the life of the process —
# fine for a tool typically restarted often in local dev; not addressed here.
_deployments_lock = threading.Lock()
_deployments: dict[str, DeploymentStatus] = {}


def _new_deployment_status() -> DeploymentStatus:
    return DeploymentStatus(steps=dict.fromkeys(DEPLOY_STEP_IDS, "pending"))


def _set_step(deployment_id: str, step_id: str, step_status: StepStatus) -> None:
    with _deployments_lock:
        _deployments[deployment_id].steps[step_id] = step_status


def _read_status(deployment_id: str) -> DeploymentStatus | None:
    with _deployments_lock:
        status = _deployments.get(deployment_id)
        return status.model_copy(deep=True) if status else None


def _run_deployment(
    deployment_id: str,
    app_session,
    request: DeployRequest,
    entry_point: str,
    app_secrets: dict,
) -> None:
    """Launches the app instance and waits for it to become healthy, in a
    background thread — independent of whether the browser stays connected:
    polling just reads whatever this last wrote, nothing about provisioning
    depends on a poll ever arriving. A bare `except Exception` at the end is
    deliberate — without it, an unexpected bug here would leave the
    deployment stuck "active" forever with no terminal state for the
    frontend to ever observe.
    """
    app_result = None
    try:
        _set_step(deployment_id, "app_compute", "active")
        app_result = launch_app_instance(
            app_session,
            deployment_id=deployment_id,
            repo_url=request.repo_url,
            entry_point=entry_point,
            instance_type=request.instance_type,
            ebs_size_gb=request.ebs_size_gb,
            secrets=app_secrets,
            instance_name=request.instance_name,
        )
        launched_at = datetime.now(timezone.utc)
        _set_step(deployment_id, "app_compute", "done")

        _set_step(deployment_id, "app_health", "active")
        wait_for_port_health(
            app_result["public_ip"], settings.app_port, timeout_seconds=settings.app_health_timeout_seconds
        )
        _set_step(deployment_id, "app_health", "done")

        app_url = f"http://{app_result['public_ip']}:{settings.app_port}"
        with _deployments_lock:
            _deployments[deployment_id].result = DeployResult(
                app_url=app_url,
                app_instance_id=app_result["instance_id"],
                instance_type=request.instance_type,
                ebs_size_gb=request.ebs_size_gb,
                launched_at=launched_at,
            )

        history.record_deployment({
            "deployment_id": deployment_id,
            "kind": "agent",
            "status": "running",
            "deployed_at": launched_at.isoformat(),
            "terminated_at": None,
            "region": request.aws.region,
            "instance_id": app_result["instance_id"],
            "instance_name": request.instance_name,
            "links": [{"label": "Agent UI", "url": app_url}],
            "decisions": {
                "repo_url": request.repo_url,
                "api_key_name": request.api_key_name,
                "base_url_name": request.base_url_name,
                "instance_type": request.instance_type,
                "ebs_size_gb": request.ebs_size_gb,
                "env_var_keys": [ev.key for ev in request.env_vars],
            },
        })
    except Exception as exc:  # deliberately broad — terminal handler for the whole background flow
        logger.exception("deployment %s failed", deployment_id)

        with _deployments_lock:
            status = _deployments[deployment_id]
            active_step = next((s for s in DEPLOY_STEP_IDS if status.steps[s] == "active"), None)
            if active_step:
                status.steps[active_step] = "error"

        # The instance succeeding on its own but never becoming healthy still
        # isn't a usable deployment — terminate it rather than leave an
        # orphaned, unhealthy instance running. A rollback failure is logged,
        # never left to raise here — this is already the terminal error
        # handler, so an unhandled exception past this point would leave the
        # deployment stuck with no error message ever recorded, worse than
        # reporting a rollback that may have failed.
        rollback_note = ""
        if app_result:
            try:
                terminate_instance(app_session, app_result["instance_id"])
            except Exception:
                logger.exception("rollback: failed to terminate app instance %s", app_result["instance_id"])
                rollback_note = (
                    f" (could not confirm rollback of {app_result['instance_id']} — check the AWS console)"
                )

        _, message = classify_error(exc)
        with _deployments_lock:
            _deployments[deployment_id].error = f"{message}{rollback_note}"


@router.post("/deploy", response_model=DeployAcceptedResponse, status_code=202)
def deploy(request: DeployRequest, background_tasks: BackgroundTasks) -> DeployAcceptedResponse:
    validation = validate_repo(request.repo_url)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    app_secrets = {ev.key: ev.value for ev in request.env_vars}
    if request.api_key_name and request.api_key_value:
        app_secrets[request.api_key_name] = request.api_key_value
    if request.base_url_name and request.base_url_value:
        app_secrets[request.base_url_name] = request.base_url_value

    try:
        app_session = get_session(request.aws.access_key_id, request.aws.secret_access_key, region=request.aws.region)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    deployment_id = uuid.uuid4().hex[:12]
    with _deployments_lock:
        _deployments[deployment_id] = _new_deployment_status()

    background_tasks.add_task(
        _run_deployment, deployment_id, app_session, request, validation.entry_point, app_secrets
    )
    return DeployAcceptedResponse(deployment_id=deployment_id)


@router.get("/deploy/{deployment_id}/status", response_model=DeploymentStatus)
def deploy_status(deployment_id: str) -> DeploymentStatus:
    status = _read_status(deployment_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Unknown deployment_id")
    return status


@router.get("/deploy/{deployment_id}/cost-estimate", response_model=CostEstimate)
def deploy_cost_estimate(deployment_id: str) -> CostEstimate:
    status = _read_status(deployment_id)
    if status is None or status.result is None:
        raise HTTPException(status_code=404, detail="Unknown or not-yet-complete deployment_id")
    result = status.result
    return CostEstimate(
        estimated_usd=estimate_ec2_cost(result.instance_type, result.ebs_size_gb, result.launched_at),
        hourly_rate=EC2_HOURLY_RATES.get(result.instance_type, 0.0),
        instance_type=result.instance_type,
        launched_at=result.launched_at,
    )


@router.post("/terminate", response_model=TerminateResponse)
def terminate(request: TerminateRequest) -> TerminateResponse:
    # No access key/secret here on purpose — this is exactly the case the local
    # credential cache (Phase 4) exists for: reauthentication for an action after
    # the initial deploy should never require re-pasting the key.
    try:
        session = get_session(region=request.region)
        terminate_instance(session, request.instance_id)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ClientError as exc:
        reraise_as_http_error(exc)

    if request.deployment_id:
        history.mark_terminated(request.deployment_id)

    return TerminateResponse(status="terminating")


def rehydrate_agent_deployments() -> None:
    """Repopulates the in-memory `_deployments` dict from disk at startup —
    without this, /status and /cost-estimate would 404 for every deployment
    made before the last backend restart, even though the EC2 instance is
    still running (see app/core/history.py)."""
    for record in history.list_deployments():
        if record.get("kind") != "agent" or record.get("status") != "running":
            continue
        deployment_id = record.get("deployment_id")
        try:
            links = record.get("links") or []
            decisions = record.get("decisions") or {}
            status = DeploymentStatus(
                steps=dict.fromkeys(DEPLOY_STEP_IDS, "done"),
                result=DeployResult(
                    app_url=links[0]["url"],
                    app_instance_id=record["instance_id"],
                    instance_type=decisions.get("instance_type", "unknown"),
                    ebs_size_gb=decisions.get("ebs_size_gb", 0),
                    launched_at=record["deployed_at"],
                ),
            )
            with _deployments_lock:
                _deployments[deployment_id] = status
        except Exception:
            logger.exception("failed to rehydrate deployment %s from history", deployment_id)
