from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.aws.feedback_stack import EBS_SIZE_GB as FEEDBACK_EBS_SIZE_GB
from app.aws.feedback_stack import INSTANCE_TYPE as FEEDBACK_INSTANCE_TYPE
from app.aws.pricing import estimate_ec2_cost, estimate_s3_cost
from app.core import history
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
        cutoff = datetime.fromisoformat(terminated_at) if terminated_at else datetime.now(timezone.utc)
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
