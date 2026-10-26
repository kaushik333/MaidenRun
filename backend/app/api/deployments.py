from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from app.api.deploy import AWS_REGIONS, resolve_ingress
from app.api.deploy_feedback import has_active_deployment
from app.aws.credentials import NoCredentialsError, get_session
from app.aws.ec2 import normalize_allowed_ips, set_security_group_cidrs
from app.aws.pricing import deployment_cost
from app.aws.sweep import delete_leftover, scan_bucket, scan_region
from app.core import feedback_client, history
from app.core.config import settings
from app.core.errors import classify_error, reraise_as_http_error
from app.core.feedback_client import FeedbackStackError
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
    # Set for agents with a fixed address (Elastic IP) — their URL survives redeploys.
    allocation_id: str | None = None
    links: list[DeploymentLink]
    # Local paths of the feedback exported right before teardown.
    exports: dict[str, str] | None = None
    decisions: dict[str, Any] = {}
    cost_estimate_usd: float | None = None


def _compute_cost(raw: dict[str, Any]) -> float | None:
    """Cost so far (see pricing.deployment_cost) — computed fresh on every
    call, never stored, since it depends on the current time."""
    try:
        return deployment_cost(raw)
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


@router.get("/deployments/{deployment_id}/export/{fmt}")
def download_export(deployment_id: str, fmt: Literal["xlsx", "json", "csv"]) -> FileResponse:
    """The feedback saved locally when this feedback stack was torn down."""
    path = ((history.get_deployment(deployment_id) or {}).get("exports") or {}).get(fmt)
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="No saved export for this deployment")
    return FileResponse(path, filename=Path(path).name)


# ---- questionnaire versions on a running feedback stack ----
# Thin pass-throughs to the stack's own admin API (see app/core/feedback_client.py)
# — publishing or activating a version changes the form at the same URL, with
# no redeploy.


def _running_feedback_stack(deployment_id: str) -> dict:
    record = history.get_deployment(deployment_id)
    if record is None or record.get("kind") != "feedback":
        raise HTTPException(status_code=404, detail="Unknown feedback deployment")
    if record.get("status") != "running":
        raise HTTPException(status_code=409, detail="This feedback stack has been terminated")
    return record


def _forward(call, *args):
    try:
        return call(*args)
    except FeedbackStackError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


class NewQuestionnaireVersion(BaseModel):
    questionnaire_json: str = Field(min_length=1)
    activate: bool = True


@router.get("/deployments/{deployment_id}/questionnaires")
def questionnaire_versions(deployment_id: str) -> dict:
    """Every questionnaire version (with response counts) and agent version."""
    return _forward(feedback_client.list_versions, _running_feedback_stack(deployment_id))


@router.post("/deployments/{deployment_id}/questionnaires", status_code=201)
def publish_questionnaire_version(deployment_id: str, request: NewQuestionnaireVersion) -> dict:
    record = _running_feedback_stack(deployment_id)
    return _forward(feedback_client.publish_questionnaire, record, request.questionnaire_json, request.activate)


@router.post("/deployments/{deployment_id}/questionnaires/{version}/activate")
def activate_questionnaire_version(deployment_id: str, version: int) -> dict:
    return _forward(feedback_client.activate_questionnaire, _running_feedback_stack(deployment_id), version)


@router.get("/deployments/{deployment_id}/questionnaires/{version}/download")
def download_questionnaire_version(deployment_id: str, version: int) -> JSONResponse:
    """A version in the same JSON format the deploy form takes — the starting
    point for editing it into a new version."""
    versions = questionnaire_versions(deployment_id)["questionnaires"]
    match = next((q for q in versions if q["version"] == version), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"No questionnaire version {version}")
    questions = [{k: v for k, v in q.items() if k != "answer"} for q in match["questions"]]
    return JSONResponse(
        {"questions": questions},
        headers={"Content-Disposition": f'attachment; filename="questionnaire-v{version}.json"'},
    )


LeftoverKind = Literal["instance", "address", "security_group", "secrets", "bucket"]


class LeftoverItem(BaseModel):
    kind: LeftoverKind
    region: str
    id: str
    label: str = ""
    detail: str = ""
    hourly_usd: float | None = None
    deployment_id: str | None = None
    # A stray feedback stack instance — deleting it loses its feedback unexported.
    destroys_feedback: bool = False


class LeftoversResponse(BaseModel):
    items: list[LeftoverItem]
    warnings: list[str] = []


class DeleteLeftoversRequest(BaseModel):
    items: list[LeftoverItem]


class DeleteResult(BaseModel):
    item: LeftoverItem
    ok: bool
    error: str | None = None


@router.get("/leftovers", response_model=LeftoversResponse)
def find_leftovers() -> LeftoversResponse:
    """Everything this platform created, in every supported region, that
    belongs to no running deployment — instances still billing, plus
    security groups, secrets and the relay bucket that outlived theirs."""
    running_ids = {r["deployment_id"] for r in history.list_deployments() if r.get("status") == "running"}
    try:
        sessions = [get_session(region=region) for region in AWS_REGIONS]
        with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
            warnings: set[str] = set()
            per_region = list(pool.map(lambda session: scan_region(session, running_ids, warnings), sessions))
        items = [item for region_items in per_region for item in region_items] + scan_bucket(sessions[0])
    except NoCredentialsError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ClientError as exc:
        reraise_as_http_error(exc)
    return LeftoversResponse(items=items, warnings=sorted(warnings))


@router.post("/leftovers/delete", response_model=list[DeleteResult])
def delete_leftovers(request: DeleteLeftoversRequest) -> list[DeleteResult]:
    """Deletes each selected item independently, reporting per-item results —
    one failure (e.g. a security group whose instance is still shutting
    down) never blocks the rest."""
    results = []
    for item in request.items:
        if item.kind == "bucket" and has_active_deployment():
            error = "A feedback-stack deploy is in progress — retry later"
            results.append(DeleteResult(item=item, ok=False, error=error))
            continue
        try:
            delete_leftover(get_session(region=item.region), item.kind, item.id)
        except NoCredentialsError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except ClientError as exc:
            # Same wording as everywhere else, e.g. an outdated IAM policy says how to fix it.
            results.append(DeleteResult(item=item, ok=False, error=classify_error(exc)[1]))
            continue
        if item.kind == "instance" and item.deployment_id and history.get_deployment(item.deployment_id):
            history.mark_terminated(item.deployment_id)
        results.append(DeleteResult(item=item, ok=True))
    return results
