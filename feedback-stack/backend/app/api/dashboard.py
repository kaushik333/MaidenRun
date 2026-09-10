from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

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
