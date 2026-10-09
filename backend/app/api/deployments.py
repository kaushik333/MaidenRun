from datetime import UTC, datetime
from typing import Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.api.deploy import resolve_ingress
from app.aws.credentials import NoCredentialsError, get_session
from app.aws.ec2 import normalize_allowed_ips, set_security_group_cidrs
from app.aws.feedback_stack import EBS_SIZE_GB as FEEDBACK_EBS_SIZE_GB
from app.aws.feedback_stack import INSTANCE_TYPE as FEEDBACK_INSTANCE_TYPE
from app.aws.pricing import estimate_ec2_cost, estimate_s3_cost
from app.core import history
from app.core.config import settings
from app.core.errors import reraise_as_http_error
from app.core.logging import logger

router = APIRouter()


class DeploymentLink(BaseModel):
    label: str
    url: str


class DeploymentRecord(BaseModel):
    deployment_id: str
    kind: Literal["agent", "feedback"]
    status: Literal["running", "terminated"]
    deployed_at: str
    terminated_at: str | None = None
    region: str
    instance_id: str
    instance_name: str | None = None
    # Only set for deployments with their own security group (i.e. an
    # editable IP allowlist) — older ones share one and have none recorded.
    security_group_id: str | None = None
    links: list[DeploymentLink]
    decisions: dict[str, Any] = {}
    cost_estimate_usd: float | None = None


def _compute_cost(raw: dict[str, Any]) -> float | None:
    """Cost incurred so far — computed fresh on every call, never stored,
    since it depends on the current time for a running deployment. For a
    terminated one, the cutoff is `terminated_at` rather than now: without
    that, a terminated instance's shown cost would keep climbing forever
    every time this endpoint is called, which is simply wrong — billing
    stopped the moment AWS tore the instance down."""
    try:
        deployed_at = datetime.fromisoformat(raw["deployed_at"])
        terminated_at = raw.get("terminated_at")
        cutoff = datetime.fromisoformat(terminated_at) if terminated_at else datetime.now(UTC)
        decisions = raw.get("decisions") or {}

        if raw["kind"] == "agent":
            instance_type = decisions.get("instance_type")
            ebs_size_gb = decisions.get("ebs_size_gb")
            if not instance_type or ebs_size_gb is None:
                return None
            return estimate_ec2_cost(instance_type, ebs_size_gb, deployed_at, now=cutoff)

        # feedback: fixed instance size, plus the (already-frozen, since the
        # bundle is deleted right after its own health check) S3 relay cost.
        ec2_cost = estimate_ec2_cost(FEEDBACK_INSTANCE_TYPE, FEEDBACK_EBS_SIZE_GB, deployed_at, now=cutoff)
        bundle_size = decisions.get("bundle_size_bytes")
        bundle_uploaded_at = decisions.get("bundle_uploaded_at")
        if not bundle_size or not bundle_uploaded_at:
            return ec2_cost
        bundle_deleted_at = decisions.get("bundle_deleted_at")
        s3_cost = estimate_s3_cost(
            bundle_size,
            datetime.fromisoformat(bundle_uploaded_at),
            datetime.fromisoformat(bundle_deleted_at) if bundle_deleted_at else None,
        )
        return ec2_cost + s3_cost
    except Exception:
        logger.exception("failed to compute cost estimate for deployment %s", raw.get("deployment_id"))
        return None


@router.get("/deployments", response_model=list[DeploymentRecord])
def list_deployments() -> list[DeploymentRecord]:
    records = []
    for raw in history.list_deployments():
        try:
            record = DeploymentRecord(**raw)
            record.cost_estimate_usd = _compute_cost(raw)
            records.append(record)
        except Exception:
            logger.exception("skipping invalid deployment history record: %s", raw.get("deployment_id"))
    return records


class AllowedIpsRequest(BaseModel):
    allowed_ips: list[str] = Field(default_factory=list)

    _normalize_allowed_ips = field_validator("allowed_ips")(normalize_allowed_ips)


@router.put("/deployments/{deployment_id}/allowed-ips", response_model=DeploymentRecord)
def update_allowed_ips(deployment_id: str, request: AllowedIpsRequest) -> DeploymentRecord:
    """Edits a running deployment's IP allowlist in place — only its security
    group's rules change, so there's no redeploy and no restart."""
    raw = history.get_deployment(deployment_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Unknown deployment_id")
    if raw.get("status") != "running" or not raw.get("security_group_id"):
        raise HTTPException(
            status_code=409,
            detail="Only running deployments made with an editable IP allowlist can be changed — redeploy older ones.",
        )

    if raw["kind"] == "agent":
        ports = [settings.app_port]
    else:
        ports = [settings.feedback_form_port, settings.feedback_dashboard_port]
    ingress = resolve_ingress(request.allowed_ips)
    try:
        set_security_group_cidrs(get_session(region=raw["region"]), raw["security_group_id"], ports, ingress)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ClientError as exc:
        reraise_as_http_error(exc)

    raw.setdefault("decisions", {})["allowed_ips"] = request.allowed_ips
    history.record_deployment(raw)
    record = DeploymentRecord(**raw)
    record.cost_estimate_usd = _compute_cost(raw)
    return record
