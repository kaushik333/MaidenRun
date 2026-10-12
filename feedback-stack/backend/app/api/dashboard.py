import csv
import io
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from app.core.config import settings
from app.db import get_connection, list_feedback

router = APIRouter()

_DASHBOARD_HTML_PATH = Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"


def _require_token(token: str | None) -> None:
    # An unset settings.dashboard_token never matches anything — the
    # dashboard is unreachable rather than silently open if it's ever
    # missing, instead of failing open.
    if not settings.dashboard_token or token != settings.dashboard_token:
        raise HTTPException(status_code=403, detail="Missing or invalid dashboard token")


def _summarize(rows: list[dict]) -> list[dict]:
    """Per-question aggregation, grouped by question *text* rather than a
    fixed schema — each row already carries its own question/options, so
    this still works even if the questionnaire was edited between
    submissions (see Phase 8's self-describing storage design)."""
    by_question: dict[str, dict] = {}
    for row in rows:
        for answer in row["answers"]:
            entry = by_question.setdefault(
                answer["question"],
                {"question": answer["question"], "options": answer.get("options"), "counts": {}, "text_answers": []},
            )
            if not answer.get("answer"):
                continue
            if answer.get("options"):
                entry["counts"][answer["answer"]] = entry["counts"].get(answer["answer"], 0) + 1
            else:
                entry["text_answers"].append(answer["answer"])
    return list(by_question.values())


@router.get("/", response_class=HTMLResponse)
def dashboard_page(token: str | None = Query(default=None)) -> HTMLResponse:
    _require_token(token)
    return HTMLResponse(_DASHBOARD_HTML_PATH.read_text())


@router.get("/api/dashboard/feedback")
def dashboard_feedback(token: str | None = Query(default=None)) -> dict:
    _require_token(token)
    with get_connection() as conn:
        rows = list_feedback(conn)
    return {"rows": rows, "summary": _summarize(rows)}


def _to_csv(rows: list[dict]) -> str:
    """One row per submission, one column per question. Columns are the union
    of every submission's questions in first-seen order (oldest first), so
    edits to the questionnaire between submissions still export cleanly —
    a question a submission didn't have is just left blank."""
    rows = sorted(rows, key=lambda row: row["submitted_at"])
    questions = list(dict.fromkeys(answer["question"] for row in rows for answer in row["answers"]))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["submitted_at", *questions])
    for row in rows:
        answers = {answer["question"]: answer.get("answer", "") for answer in row["answers"]}
        writer.writerow([row["submitted_at"], *(answers.get(q, "") for q in questions)])
    return buffer.getvalue()


@router.get("/api/dashboard/export")
def dashboard_export(
    token: str | None = Query(default=None), format: Literal["csv", "json"] = Query(default="csv")
) -> Response:
    """Every submission as a downloadable file — CSV for spreadsheets, JSON
    for the raw stored records. Also what the control plane saves locally
    before tearing the feedback stack down."""
    _require_token(token)
    with get_connection() as conn:
        rows = list_feedback(conn)
    if format == "csv":
        body, media_type = _to_csv(rows), "text/csv"
    else:
        body, media_type = json.dumps(rows, indent=2), "application/json"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="feedback-{settings.deployment_id}.{format}"'},
    )
