import threading
import uuid
from typing import Literal, NoReturn

from botocore.exceptions import ClientError
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, model_validator

from app.aws.credentials import NoCredentialsError, get_session
from app.aws.ec2 import (
    PROVIDER_ENV_VARS,
    Ec2LaunchError,
    HealthCheckTimeoutError,
    launch_app_instance,
    terminate_instance,
    wait_for_port_health,
)
from app.core.config import settings
from app.core.logging import logger
from app.validation.validator import validate_repo

GPU_INSTANCE_TYPES = ("g4dn.xlarge", "g5.xlarge")
CPU_INSTANCE_TYPES = ("t3.small", "t3.medium", "m5.xlarge")
LLM_PROVIDERS = ("openai", "anthropic", "azure_openai", "openrouter")
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

# AWS error codes that actually mean "this access key/secret is wrong" — not
# every ClientError is a credentials problem (e.g. a bad instance profile
# name isn't), so only these get the "AWS rejected these credentials" framing.
CREDENTIAL_ERROR_CODES = {
    "UnrecognizedClientException",
    "InvalidClientTokenId",
    "AuthFailure",
    "SignatureDoesNotMatch",
    "AccessDenied",
    "AccessDeniedException",
}

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
    self_hosted_llm: bool
    instance_type: str
    ebs_size_gb: Literal[20, 50, 100]
    instance_name: str | None = None
    llm_provider: Literal["openai", "anthropic", "azure_openai", "openrouter"] | None = None
    api_key: str | None = None
    env_vars: list[EnvVar] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_branch_fields(self) -> "DeployRequest":
        if self.self_hosted_llm:
            if self.instance_type not in GPU_INSTANCE_TYPES:
                raise ValueError(
                    f"instance_type must be one of {GPU_INSTANCE_TYPES} when self_hosted_llm is true"
                )
            if self.llm_provider is not None or self.api_key is not None:
                raise ValueError(
                    "llm_provider and api_key must be omitted when self_hosted_llm is true"
                )
        else:
            if self.instance_type not in CPU_INSTANCE_TYPES:
                raise ValueError(
                    f"instance_type must be one of {CPU_INSTANCE_TYPES} when self_hosted_llm is false"
                )
            if self.llm_provider is None:
                raise ValueError("llm_provider is required when self_hosted_llm is false")
            if not self.api_key:
                raise ValueError("api_key is required when self_hosted_llm is false")
        return self


class DeployResult(BaseModel):
    """What a deployment produces once every step has actually succeeded —
    the "result" field of DeploymentStatus once polling reaches "done"."""

    app_url: str
    app_instance_id: str


class DeployAcceptedResponse(BaseModel):
    deployment_id: str


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


class TerminateResponse(BaseModel):
    status: str


def _classify_error(exc: Exception) -> tuple[int, str]:
    """Maps any exception from a provisioning step to (status_code, detail).

    The status_code half is only meaningful for /terminate's synchronous
    error responses now — a background deployment's failure is reported via
    the polled JSON body, not an HTTP error status, but the same message
    classification still applies: same distinction throughout between 'your
    credentials are wrong' and 'AWS rejected this specific request' for some
    other reason (see CREDENTIAL_ERROR_CODES). Anything not otherwise
    recognized still gets classified rather than left to propagate
    unhandled — an unhandled exception's response skips CORSMiddleware
    entirely and the browser reports it as a bare network failure, hiding
    whatever actually went wrong.
    """
    if isinstance(exc, NoCredentialsError):
        return 401, str(exc)
    if isinstance(exc, ClientError):
        code = exc.response["Error"]["Code"]
        message = exc.response["Error"]["Message"]
        if code in CREDENTIAL_ERROR_CODES:
            return 401, f"AWS rejected these credentials: {message}"
        return 400, f"AWS rejected the request: {message}"
    # Checked before the (broader) Ec2LaunchError below, since it's a subclass:
    # provisioned fine, just never answered — a narrower, 504-shaped problem.
    if isinstance(exc, HealthCheckTimeoutError):
        return 504, str(exc)
    if isinstance(exc, Ec2LaunchError):
        return 502, str(exc)
    return 500, str(exc)


def _reraise_as_http_error(exc: ClientError) -> NoReturn:
    """Thin wrapper for the single-exception call sites (/terminate)."""
    logger.exception("AWS rejected the request")
    status_code, detail = _classify_error(exc)
    raise HTTPException(status_code=status_code, detail=detail) from exc


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
        _set_step(deployment_id, "app_compute", "done")

        _set_step(deployment_id, "app_health", "active")
        wait_for_port_health(
            app_result["public_ip"], settings.app_port, timeout_seconds=settings.app_health_timeout_seconds
        )
        _set_step(deployment_id, "app_health", "done")

        with _deployments_lock:
            _deployments[deployment_id].result = DeployResult(
                app_url=f"http://{app_result['public_ip']}:{settings.app_port}",
                app_instance_id=app_result["instance_id"],
            )
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

        _, message = _classify_error(exc)
        with _deployments_lock:
            _deployments[deployment_id].error = f"{message}{rollback_note}"


@router.post("/deploy", response_model=DeployAcceptedResponse, status_code=202)
def deploy(request: DeployRequest, background_tasks: BackgroundTasks) -> DeployAcceptedResponse:
    validation = validate_repo(request.repo_url)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)

    # Self-hosted LLM: no provider secret to inject; just whatever advanced env vars were given.
    app_secrets = {ev.key: ev.value for ev in request.env_vars}
    if not request.self_hosted_llm:
        app_secrets[PROVIDER_ENV_VARS[request.llm_provider]] = request.api_key

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
        _reraise_as_http_error(exc)

    return TerminateResponse(status="terminating")
