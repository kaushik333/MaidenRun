import re
import threading
import uuid
from datetime import UTC, datetime
from typing import Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.aws.credentials import NoCredentialsError, get_session
from app.aws.ec2 import (
    REPO_TOKEN_PARAM_NAME,
    address_ip,
    allocate_address,
    delete_secret_parameters,
    delete_security_group_after_termination,
    ingress_cidrs,
    launch_app_instance,
    move_address,
    normalize_allowed_ips,
    release_address,
    terminate_instance,
    wait_for_port_health,
)
from app.aws.pricing import deployment_cost, estimate_ec2_cost, instance_hourly_rate
from app.core import history
from app.core.config import settings
from app.core.errors import classify_error, reraise_as_http_error
from app.core.feedback_client import FeedbackStackError, announce_agent_version, export_feedback
from app.core.logging import logger
from app.validation.models import ValidateRepoResponse
from app.validation.validator import validate_repo

CPU_INSTANCE_TYPES = ("t3.small", "t3.medium", "m5.xlarge")
ENV_VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
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
    # Optional: omitted when the backend has an AWS profile or a cached key
    # (see get_session for the order credentials are looked up in).
    access_key_id: str | None = None
    secret_access_key: str | None = None
    region: Literal[
        "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1",
        "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "me-central-1",
    ] = "us-east-1"


class EnvVar(BaseModel):
    key: str = Field(min_length=1)
    value: str


class DeployRequest(BaseModel):
    repo_url: str = Field(min_length=1)
    # A GitHub personal access token for a private repo_url — optional,
    # treated exactly like every other secret here: never logged, never
    # persisted to the deployment history, delivered to the instance only
    # via the same SSM-parameter mechanism as the agent's own secrets.
    github_token: str | None = None
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
    # IPv4 addresses/ranges allowed to reach :8001 — empty means everyone.
    allowed_ips: list[str] = Field(default_factory=list)

    _normalize_allowed_ips = field_validator("allowed_ips")(normalize_allowed_ips)

    @model_validator(mode="after")
    def check_fields(self) -> "DeployRequest":
        if self.instance_type not in CPU_INSTANCE_TYPES:
            raise ValueError(f"instance_type must be one of {CPU_INSTANCE_TYPES}")
        if bool(self.api_key_name) != bool(self.api_key_value):
            raise ValueError("api_key_name and api_key_value must be provided together")
        if bool(self.base_url_name) != bool(self.base_url_value):
            raise ValueError("base_url_name and base_url_value must be provided together")
        # Names are spliced into the instance's boot script as `export NAME=...`
        # and into its compose override — anything but a plain env var name
        # would break (or inject into) that script.
        names = [ev.key for ev in self.env_vars] + [n for n in (self.api_key_name, self.base_url_name) if n]
        invalid = [n for n in names if not ENV_VAR_NAME.fullmatch(n)]
        if invalid:
            raise ValueError(
                f"Invalid environment variable name(s): {', '.join(invalid)} — use letters, digits and "
                "underscores only, not starting with a digit"
            )
        if len(set(names)) != len(names) or REPO_TOKEN_PARAM_NAME in names:
            raise ValueError("Environment variable names must be unique")
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
    # A feedback stack's data is exported before teardown, and teardown stops
    # (409) if that fails. True = the user chose to terminate anyway.
    force: bool = False


class TerminateResponse(BaseModel):
    status: str
    # Local paths of the feedback exported before teardown, when there was any.
    exports: dict[str, str] | None = None


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
    validation: ValidateRepoResponse,
    app_secrets: dict,
    ingress: list[str],
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
    allocation_id = None
    try:
        _set_step(deployment_id, "app_compute", "active")
        app_result = launch_app_instance(
            app_session,
            deployment_id=deployment_id,
            repo_url=request.repo_url,
            entry_point=validation.entry_point,
            instance_type=request.instance_type,
            ebs_size_gb=request.ebs_size_gb,
            secrets=app_secrets,
            instance_name=request.instance_name,
            github_token=request.github_token,
            ingress=ingress,
            commit_sha=validation.commit_sha,
        )
        # The fixed address this agent keeps for life — redeploys move it to
        # the new instance, so the URL never changes.
        allocation_id, public_ip = allocate_address(app_session, deployment_id)
        move_address(app_session, allocation_id, app_result["instance_id"])
        launched_at = datetime.now(UTC)
        _set_step(deployment_id, "app_compute", "done")

        _set_step(deployment_id, "app_health", "active")
        wait_for_port_health(public_ip, settings.app_port, timeout_seconds=settings.app_health_timeout_seconds)
        _set_step(deployment_id, "app_health", "done")

        app_url = f"http://{public_ip}:{settings.app_port}"
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
            "security_group_id": app_result["security_group_id"],
            "allocation_id": allocation_id,
            "instance_name": request.instance_name,
            "links": [{"label": "Agent UI", "url": app_url}],
            "decisions": {
                "repo_url": request.repo_url,
                "private_repo": bool(request.github_token),
                "api_key_name": request.api_key_name,
                "base_url_name": request.base_url_name,
                "instance_type": request.instance_type,
                "ebs_size_gb": request.ebs_size_gb,
                "env_var_keys": [ev.key for ev in request.env_vars],
                "allowed_ips": request.allowed_ips,
                "commit_sha": validation.commit_sha,
                "commit_message": validation.commit_message,
            },
        })
        # Feedback stacks collecting feedback for this repo now stamp new
        # responses with this commit.
        if validation.commit_sha:
            announce_agent_version(request.repo_url, validation.commit_sha, validation.commit_message or "")
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
        if allocation_id:
            try:
                release_address(app_session, allocation_id)
            except Exception:
                logger.exception("rollback: failed to release address %s", allocation_id)

        _, message = classify_error(exc)
        with _deployments_lock:
            _deployments[deployment_id].error = f"{message}{rollback_note}"
        if app_result:
            delete_security_group_after_termination(
                app_session, app_result["security_group_id"], app_result["instance_id"]
            )


@router.post("/deploy", response_model=DeployAcceptedResponse, status_code=202)
def deploy(request: DeployRequest, background_tasks: BackgroundTasks) -> DeployAcceptedResponse:
    validation = validate_repo(request.repo_url, request.github_token)
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
    ingress = resolve_ingress(request.allowed_ips)

    deployment_id = uuid.uuid4().hex[:12]
    with _deployments_lock:
        _deployments[deployment_id] = _new_deployment_status()

    background_tasks.add_task(
        _run_deployment, deployment_id, app_session, request, validation, app_secrets, ingress
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
    record = history.get_deployment(deployment_id)
    return CostEstimate(
        # Same total as the Deployments tab: from the first deploy, across redeploys.
        estimated_usd=deployment_cost(record) if record else estimate_ec2_cost(
            result.instance_type, result.ebs_size_gb, result.launched_at
        ),
        hourly_rate=instance_hourly_rate(result.instance_type),
        instance_type=result.instance_type,
        launched_at=result.launched_at,
    )


@router.post("/terminate", response_model=TerminateResponse)
def terminate(request: TerminateRequest, background_tasks: BackgroundTasks) -> TerminateResponse:
    # No access key/secret here on purpose: get_session() uses the AWS profile or
    # the key saved on first deploy, so acting on a deployment never re-asks for it.
    record = history.get_deployment(request.deployment_id) if request.deployment_id else None
    with _deployments_lock:
        if request.deployment_id in _redeploying:
            detail = "A redeploy of this agent is in progress — wait for it to finish, then terminate."
            raise HTTPException(status_code=409, detail=detail)

    # Feedback lives only on the instance's own disk — copy it out first, and
    # refuse to destroy it silently if that fails.
    exports = None
    if record and record.get("kind") == "feedback" and not request.force:
        try:
            exports = export_feedback(record)
        except (FeedbackStackError, OSError) as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Couldn't export this deployment's feedback before terminating: {exc}. "
                "Nothing was deleted — retry, or terminate anyway to delete the feedback without a copy.",
            ) from exc

    # History is authoritative for which instance a deployment runs on right
    # now — after a redeploy, a page opened earlier still holds the old id.
    instance_id = record["instance_id"] if record else request.instance_id
    try:
        session = get_session(region=request.region)
        terminate_instance(session, instance_id)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ClientError as exc:
        reraise_as_http_error(exc)

    if request.deployment_id:
        history.mark_terminated(request.deployment_id, exports=exports)
        allocation_id = (record or {}).get("allocation_id")
        if allocation_id:
            try:
                release_address(session, allocation_id)  # an unattached address keeps billing
            except ClientError:
                logger.exception("could not release address %s — the leftover sweep lists it", allocation_id)
        try:
            delete_secret_parameters(session, request.deployment_id)
        except ClientError:
            # The instance is already gone, so don't fail the request over
            # this — "Find leftover resources" lists any secrets left behind.
            logger.exception("could not delete secrets for deployment %s", request.deployment_id)
        # Deployments made before per-deployment security groups share one
        # and have no id recorded — that shared group is left alone.
        group_id = (record or {}).get("security_group_id")
        if group_id:
            background_tasks.add_task(delete_security_group_after_termination, session, group_id, instance_id)

    return TerminateResponse(status="terminating", exports=exports)


# ---- redeploy: a new commit at the same URL ----

REDEPLOY_STEP_IDS = ("app_compute", "app_health", "switch")
# Agent deployments with a redeploy in flight (guarded by _deployments_lock) —
# two at once would race for the same fixed address.
_redeploying: set[str] = set()


class RedeployRequest(BaseModel):
    # Only needed for a private repo: the control plane deliberately can't read
    # the stored token back, and needs one to validate the new commit.
    github_token: str | None = None


def _secret_names(decisions: dict) -> list[str]:
    names = [decisions.get("api_key_name"), decisions.get("base_url_name"), *(decisions.get("env_var_keys") or [])]
    return [name for name in names if name]


@router.post("/deployments/{deployment_id}/redeploy", response_model=DeployAcceptedResponse, status_code=202)
def redeploy(deployment_id: str, request: RedeployRequest, background_tasks: BackgroundTasks) -> DeployAcceptedResponse:
    """Deploys the latest commit of a running agent's repo — same settings,
    same secrets, same URL. The new instance is health-checked while the old
    one keeps serving; only then does the agent's fixed address move over.
    Returns an id to poll at /deploy/<id>/status."""
    record = history.get_deployment(deployment_id)
    if record is None or record.get("kind") != "agent":
        raise HTTPException(status_code=404, detail="Unknown agent deployment")
    if record.get("status") != "running":
        raise HTTPException(status_code=409, detail="This agent has been terminated — deploy it again instead")
    decisions = record.get("decisions") or {}

    validation = validate_repo(decisions["repo_url"], request.github_token)
    if not validation.valid:
        raise HTTPException(status_code=400, detail=validation.error)
    try:
        session = get_session(region=record["region"])
    except NoCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    # Only used when an older deployment without its own security group gets one now.
    ingress = resolve_ingress(decisions.get("allowed_ips") or [])

    with _deployments_lock:
        if deployment_id in _redeploying:
            raise HTTPException(status_code=409, detail="A redeploy of this agent is already in progress")
        _redeploying.add(deployment_id)
        redeploy_id = uuid.uuid4().hex[:12]
        _deployments[redeploy_id] = DeploymentStatus(steps=dict.fromkeys(REDEPLOY_STEP_IDS, "pending"))

    background_tasks.add_task(
        _run_redeploy, redeploy_id, session, record, validation, request.github_token, ingress
    )
    return DeployAcceptedResponse(deployment_id=redeploy_id)


def _run_redeploy(
    redeploy_id: str,
    session,
    record: dict,
    validation: ValidateRepoResponse,
    github_token: str | None,
    ingress: list[str],
) -> None:
    """Blue/green: launch the new commit beside the running one, wait until it
    answers, move the fixed address onto it, then terminate the old instance.
    Any failure before the move leaves the old version serving untouched."""
    deployment_id = record["deployment_id"]
    decisions = record["decisions"]
    new = None
    allocated_here = None
    extra_started = None  # when the second instance started billing alongside the first
    try:
        _set_step(redeploy_id, "app_compute", "active")
        new = launch_app_instance(
            session,
            deployment_id=deployment_id,
            repo_url=decisions["repo_url"],
            entry_point=validation.entry_point,
            instance_type=decisions["instance_type"],
            ebs_size_gb=decisions["ebs_size_gb"],
            secrets={},
            existing_secret_names=_secret_names(decisions),
            instance_name=record.get("instance_name"),
            github_token=github_token,
            ingress=ingress,
            commit_sha=validation.commit_sha,
            security_group_id=record.get("security_group_id"),
        )
        extra_started = datetime.now(UTC)
        _set_step(redeploy_id, "app_compute", "done")

        # Checked on the new instance's own temporary address — reviewers are
        # still on the old one via the fixed address.
        _set_step(redeploy_id, "app_health", "active")
        wait_for_port_health(new["public_ip"], settings.app_port, timeout_seconds=settings.app_health_timeout_seconds)
        _set_step(redeploy_id, "app_health", "done")

        _set_step(redeploy_id, "switch", "active")
        allocation_id = record.get("allocation_id")
        if allocation_id:
            public_ip = address_ip(session, allocation_id)
        else:  # deployed before fixed addresses: its URL changes this one time
            allocation_id, public_ip = allocated_here = allocate_address(session, deployment_id)
        move_address(session, allocation_id, new["instance_id"])
        old_instance_id = record["instance_id"]
        terminate_instance(session, old_instance_id)
        _set_step(redeploy_id, "switch", "done")

        now = datetime.now(UTC)
        app_url = f"http://{public_ip}:{settings.app_port}"
        # Both instances billed from the new one's launch until the old one stopped.
        record.setdefault("extra_runs", []).append(
            {"started_at": extra_started.isoformat(), "ended_at": now.isoformat(), "outcome": "redeployed"}
        )
        record.setdefault("revisions", []).append({
            "commit_sha": decisions.get("commit_sha"),
            "commit_message": decisions.get("commit_message"),
            "instance_id": old_instance_id,
            "replaced_at": now.isoformat(),
        })
        record.update(
            instance_id=new["instance_id"],
            security_group_id=new["security_group_id"],
            allocation_id=allocation_id,
            links=[{"label": "Agent UI", "url": app_url}],
        )
        decisions.update(
            commit_sha=validation.commit_sha, commit_message=validation.commit_message, redeployed_at=now.isoformat()
        )
        history.record_deployment(record)

        result = DeployResult(
            app_url=app_url,
            app_instance_id=new["instance_id"],
            instance_type=decisions["instance_type"],
            ebs_size_gb=decisions["ebs_size_gb"],
            launched_at=now,
        )
        with _deployments_lock:
            _deployments[redeploy_id].result = result
            if deployment_id in _deployments and _deployments[deployment_id].result:
                # So the original deploy's cost monitor follows the new instance.
                _deployments[deployment_id].result.app_instance_id = new["instance_id"]
                _deployments[deployment_id].result.app_url = app_url
        if validation.commit_sha:
            announce_agent_version(decisions["repo_url"], validation.commit_sha, validation.commit_message or "")
    except Exception as exc:  # deliberately broad — terminal handler for the whole background flow
        logger.exception("redeploy %s of %s failed", redeploy_id, deployment_id)
        with _deployments_lock:
            status = _deployments[redeploy_id]
            active_step = next((s for s in REDEPLOY_STEP_IDS if status.steps[s] == "active"), None)
            if active_step:
                status.steps[active_step] = "error"
        _, message = classify_error(exc)
        with _deployments_lock:
            _deployments[redeploy_id].error = f"{message.rstrip('.')}. The previous version is still serving."
        # Roll back only what this redeploy created — the old instance and the
        # fixed address it serves on stay exactly as they were.
        if new:
            try:
                terminate_instance(session, new["instance_id"])
                # The failed attempt still billed while it ran — add it to the deployment's total.
                current = history.get_deployment(deployment_id)
                if current and extra_started:
                    current.setdefault("extra_runs", []).append({
                        "started_at": extra_started.isoformat(),
                        "ended_at": datetime.now(UTC).isoformat(),
                        "outcome": "failed",
                    })
                    history.record_deployment(current)
                if not record.get("security_group_id"):
                    delete_security_group_after_termination(session, new["security_group_id"], new["instance_id"])
            except Exception:
                logger.exception("rollback: failed to remove new instance %s", new["instance_id"])
        if allocated_here:
            try:
                release_address(session, allocated_here[0])
            except Exception:
                logger.exception("rollback: failed to release address %s", allocated_here[0])
    finally:
        with _deployments_lock:
            _redeploying.discard(deployment_id)


def resolve_ingress(allowed_ips: list[str]) -> list[str]:
    """ingress_cidrs() with its one network failure mode turned into a clear
    error before anything touches AWS. Shared by both deploy endpoints and
    the allowed-IPs editor."""
    try:
        return ingress_cidrs(allowed_ips)
    except OSError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not look up your public IP (needed so an IP allowlist never locks you out): {exc}",
        ) from exc


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
