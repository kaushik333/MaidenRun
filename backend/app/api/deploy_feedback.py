import json
import threading
import uuid
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api.deploy import resolve_ingress
from app.aws.credentials import NoCredentialsError, get_session
from app.aws.ec2 import (
    HealthCheckTimeoutError,
    delete_security_group_after_termination,
    normalize_allowed_ips,
    terminate_instance,
    wait_for_port_health,
)
from app.aws.feedback_stack import EBS_SIZE_GB, INSTANCE_TYPE, launch_feedback_stack
from app.aws.pricing import EC2_HOURLY_RATES, estimate_ec2_cost, estimate_s3_cost
from app.aws.s3_bundle import (
    BUNDLE_KEY_PREFIX,
    build_bundle,
    delete_bundle,
    ensure_bundle_bucket,
    upload_bundle,
)
from app.core import history
from app.core.config import settings
from app.core.errors import classify_error
from app.core.logging import logger

router = APIRouter()


class FeedbackAwsCredentials(BaseModel):
    # Optional, unlike the main /deploy's AwsCredentials — get_session()
    # falls back to whatever was cached by an earlier action (deploy or a
    # previous feedback-stack deploy), so this can be the very first AWS
    # action a user ever takes, or a later one that reuses what's cached.
    access_key_id: str | None = None
    secret_access_key: str | None = None
    region: Literal[
        "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "eu-central-1",
        "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "me-central-1",
    ] = "us-east-1"


class DeployFeedbackRequest(BaseModel):
    aws: FeedbackAwsCredentials
    instance_name: str | None = None
    # Raw text of the uploaded questionnaire file — parsing/rendering it is
    # the feedback-form app's job (see feedback-stack/), not the control
    # plane's; this layer only confirms it's non-empty, valid JSON before
    # ever touching AWS.
    questionnaire_json: str = Field(min_length=1)
    # IPv4 addresses/ranges allowed to reach :8002/:8003 — empty means everyone.
    allowed_ips: list[str] = Field(default_factory=list)

    _normalize_allowed_ips = field_validator("allowed_ips")(normalize_allowed_ips)


class DeployFeedbackResult(BaseModel):
    feedback_form_url: str
    feedback_dashboard_url: str
    feedback_instance_id: str
    launched_at: datetime
    bundle_size_bytes: int
    bundle_uploaded_at: datetime
    bundle_deleted_at: datetime | None = None


class DeployFeedbackAcceptedResponse(BaseModel):
    deployment_id: str


class FeedbackCostEstimate(BaseModel):
    ec2_estimated_usd: float
    s3_estimated_usd: float
    hourly_rate: float
    instance_type: str
    launched_at: datetime


StepStatus = Literal["pending", "active", "done", "error"]
FEEDBACK_STEP_IDS = ("feedback_bundle", "feedback_compute", "feedback_health")


class FeedbackDeploymentStatus(BaseModel):
    steps: dict[str, StepStatus]
    result: DeployFeedbackResult | None = None
    error: str | None = None


# Independent id space and store from app deployments (app.api.deploy) —
# deploying the feedback stack has nothing to do with any particular app
# deployment, so it isn't keyed off one.
_feedback_deployments_lock = threading.Lock()
_feedback_deployments: dict[str, FeedbackDeploymentStatus] = {}


def _new_feedback_status() -> FeedbackDeploymentStatus:
    return FeedbackDeploymentStatus(steps=dict.fromkeys(FEEDBACK_STEP_IDS, "pending"))


def _set_step(deployment_id: str, step_id: str, step_status: StepStatus) -> None:
    with _feedback_deployments_lock:
        _feedback_deployments[deployment_id].steps[step_id] = step_status


def _read_status(deployment_id: str) -> FeedbackDeploymentStatus | None:
    with _feedback_deployments_lock:
        status = _feedback_deployments.get(deployment_id)
        return status.model_copy(deep=True) if status else None


def has_active_deployment() -> bool:
    """True while any feedback-stack deployment is still mid-flight (has
    neither succeeded nor failed yet) — used to block S3 bucket cleanup so
    it can never race a deploy that's actively uploading to or reading from
    that same bucket."""
    with _feedback_deployments_lock:
        return any(s.result is None and s.error is None for s in _feedback_deployments.values())


def _run_feedback_deployment(
    deployment_id: str,
    session,
    region: str,
    instance_name: str | None,
    questionnaire_json: str,
    allowed_ips: list[str],
    ingress: list[str],
) -> None:
    """Uploads the bundle, launches the feedback-stack instance, and waits
    for it to become healthy — in a background thread, independent of
    whether the browser stays connected. A bare `except Exception` at the
    end is deliberate — without it, an unexpected bug here would leave the
    deployment stuck "active" forever with no terminal state for the
    frontend to ever observe.
    """
    bucket = None
    key = None
    feedback_result = None
    try:
        _set_step(deployment_id, "feedback_bundle", "active")
        bucket = ensure_bundle_bucket(session)
        key = f"{BUNDLE_KEY_PREFIX}{deployment_id}.tar.gz"
        bundle_bytes = build_bundle(questionnaire_json)
        upload_bundle(session, bucket, key, bundle_bytes)
        bundle_size_bytes = len(bundle_bytes)
        bundle_uploaded_at = datetime.now(UTC)
        _set_step(deployment_id, "feedback_bundle", "done")

        _set_step(deployment_id, "feedback_compute", "active")
        feedback_result = launch_feedback_stack(
            session, deployment_id=deployment_id, bucket=bucket, key=key, instance_name=instance_name, ingress=ingress
        )
        launched_at = datetime.now(UTC)
        _set_step(deployment_id, "feedback_compute", "done")

        _set_step(deployment_id, "feedback_health", "active")
        wait_for_port_health(
            feedback_result["public_ip"], settings.feedback_form_port,
            timeout_seconds=settings.app_health_timeout_seconds,
        )
        wait_for_port_health(
            feedback_result["public_ip"], settings.feedback_dashboard_port,
            timeout_seconds=settings.app_health_timeout_seconds,
        )
        _set_step(deployment_id, "feedback_health", "done")

        # The instance has necessarily already downloaded the bundle by now
        # (a strictly earlier step in its own boot script than answering on
        # either port), so it's safe to clean up here — no race.
        delete_bundle(session, bucket, key)
        bundle_deleted_at = datetime.now(UTC)

        feedback_form_url = f"http://{feedback_result['public_ip']}:{settings.feedback_form_port}"
        feedback_dashboard_url = (
            f"http://{feedback_result['public_ip']}:{settings.feedback_dashboard_port}"
            f"/?token={feedback_result['dashboard_token']}"
        )
        with _feedback_deployments_lock:
            _feedback_deployments[deployment_id].result = DeployFeedbackResult(
                feedback_form_url=feedback_form_url,
                feedback_dashboard_url=feedback_dashboard_url,
                feedback_instance_id=feedback_result["instance_id"],
                launched_at=launched_at,
                bundle_size_bytes=bundle_size_bytes,
                bundle_uploaded_at=bundle_uploaded_at,
                bundle_deleted_at=bundle_deleted_at,
            )

        history.record_deployment({
            "deployment_id": deployment_id,
            "kind": "feedback",
            "status": "running",
            "deployed_at": launched_at.isoformat(),
            "terminated_at": None,
            "region": region,
            "instance_id": feedback_result["instance_id"],
            "security_group_id": feedback_result["security_group_id"],
            "instance_name": instance_name,
            "links": [
                {"label": "Feedback form", "url": feedback_form_url},
                {"label": "Feedback dashboard", "url": feedback_dashboard_url},
            ],
            "decisions": {
                "bundle_size_bytes": bundle_size_bytes,
                "bundle_uploaded_at": bundle_uploaded_at.isoformat(),
                "bundle_deleted_at": bundle_deleted_at.isoformat(),
                "allowed_ips": allowed_ips,
            },
        })
    except Exception as exc:  # deliberately broad — terminal handler for the whole background flow
        logger.exception("feedback-stack deployment %s failed", deployment_id)

        with _feedback_deployments_lock:
            status = _feedback_deployments[deployment_id]
            active_step = next((s for s in FEEDBACK_STEP_IDS if status.steps[s] == "active"), None)
            if active_step:
                status.steps[active_step] = "error"

        # Never leave the instance or the bundle behind on failure. Both
        # cleanups are logged, never left to raise here — this is already
        # the terminal error handler, so an unhandled exception past this
        # point would leave the deployment stuck with no error message ever
        # recorded, worse than reporting a cleanup that may have failed.
        rollback_note = ""
        if feedback_result:
            try:
                terminate_instance(session, feedback_result["instance_id"])
            except Exception:
                logger.exception(
                    "rollback: failed to terminate feedback instance %s", feedback_result["instance_id"]
                )
                rollback_note = (
                    f" (could not confirm rollback of {feedback_result['instance_id']} — check the AWS console)"
                )
        if bucket and key:
            delete_bundle(session, bucket, key)

        _, message = classify_error(exc)
        if isinstance(exc, HealthCheckTimeoutError):
            # The instance's own boot script needs s3:GetObject (see
            # infra/instance-role-policy.json) to pull its bundle — a common,
            # easy-to-miss cause of exactly this symptom is that permission
            # having drifted out of sync on the instance role specifically
            # (a separate AWS identity from the IAM user's own policy, which
            # is what usually gets re-synced first).
            message += (
                " If this keeps happening, check that maidenrun-instance-role's "
                "policy in AWS matches the current infra/instance-role-policy.json — a missing "
                "s3:GetObject permission there produces exactly this timeout, since the instance "
                "can never download its own bundle."
            )
        with _feedback_deployments_lock:
            _feedback_deployments[deployment_id].error = f"{message}{rollback_note}"
        if feedback_result:
            delete_security_group_after_termination(
                session, feedback_result["security_group_id"], feedback_result["instance_id"]
            )


@router.post("/deploy-feedback", response_model=DeployFeedbackAcceptedResponse, status_code=202)
def deploy_feedback(
    request: DeployFeedbackRequest, background_tasks: BackgroundTasks
) -> DeployFeedbackAcceptedResponse:
    try:
        json.loads(request.questionnaire_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Questionnaire file is not valid JSON: {exc}") from exc

    try:
        session = get_session(request.aws.access_key_id, request.aws.secret_access_key, region=request.aws.region)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    ingress = resolve_ingress(request.allowed_ips)

    deployment_id = uuid.uuid4().hex[:12]
    with _feedback_deployments_lock:
        _feedback_deployments[deployment_id] = _new_feedback_status()

    background_tasks.add_task(
        _run_feedback_deployment,
        deployment_id,
        session,
        request.aws.region,
        request.instance_name,
        request.questionnaire_json,
        request.allowed_ips,
        ingress,
    )
    return DeployFeedbackAcceptedResponse(deployment_id=deployment_id)


@router.get("/deploy-feedback/{deployment_id}/status", response_model=FeedbackDeploymentStatus)
def deploy_feedback_status(deployment_id: str) -> FeedbackDeploymentStatus:
    status = _read_status(deployment_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Unknown deployment_id")
    return status


@router.get("/deploy-feedback/{deployment_id}/cost-estimate", response_model=FeedbackCostEstimate)
def deploy_feedback_cost_estimate(deployment_id: str) -> FeedbackCostEstimate:
    status = _read_status(deployment_id)
    if status is None or status.result is None:
        raise HTTPException(status_code=404, detail="Unknown or not-yet-complete deployment_id")
    result = status.result
    return FeedbackCostEstimate(
        ec2_estimated_usd=estimate_ec2_cost(INSTANCE_TYPE, EBS_SIZE_GB, result.launched_at),
        s3_estimated_usd=estimate_s3_cost(
            result.bundle_size_bytes, result.bundle_uploaded_at, result.bundle_deleted_at
        ),
        hourly_rate=EC2_HOURLY_RATES.get(INSTANCE_TYPE, 0.0),
        instance_type=INSTANCE_TYPE,
        launched_at=result.launched_at,
    )


def rehydrate_feedback_deployments() -> None:
    """Repopulates the in-memory `_feedback_deployments` dict from disk at
    startup — see rehydrate_agent_deployments() in app/api/deploy.py for why
    this is needed."""
    for record in history.list_deployments():
        if record.get("kind") != "feedback" or record.get("status") != "running":
            continue
        deployment_id = record.get("deployment_id")
        try:
            links = {link["label"]: link["url"] for link in record.get("links") or []}
            decisions = record.get("decisions") or {}
            status = FeedbackDeploymentStatus(
                steps=dict.fromkeys(FEEDBACK_STEP_IDS, "done"),
                result=DeployFeedbackResult(
                    feedback_form_url=links["Feedback form"],
                    feedback_dashboard_url=links["Feedback dashboard"],
                    feedback_instance_id=record["instance_id"],
                    launched_at=record["deployed_at"],
                    bundle_size_bytes=decisions.get("bundle_size_bytes", 0),
                    bundle_uploaded_at=decisions.get("bundle_uploaded_at", record["deployed_at"]),
                    bundle_deleted_at=decisions.get("bundle_deleted_at"),
                ),
            )
            with _feedback_deployments_lock:
                _feedback_deployments[deployment_id] = status
        except Exception:
            logger.exception("failed to rehydrate feedback deployment %s from history", deployment_id)
