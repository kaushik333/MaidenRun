import csv
import io
import json
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dashboard import router as dashboard_router
from app.core.config import settings
from app.db import ensure_schema, get_connection, insert_feedback

# A standalone app around just the dashboard router, rather than the full
# app.main — SERVICE_ROLE (which router main.py wires up at `/`) is read
# once at import time via a module-level singleton, so a single pytest
# session can't flip it per-test. Testing the router directly sidesteps
# that and is arguably the more standard boundary anyway.
app = FastAPI()
app.include_router(dashboard_router)
client = TestClient(app)

SUBMISSION = [
    {
        "question": "Did the agent understand your request correctly?",
        "answer": "Yes",
        "options": ["Yes", "Partially", "No"],
        "optional": False,
    },
    {
        "question": "Did the agent understand your request correctly?",
        "answer": "No",
        "options": ["Yes", "Partially", "No"],
        "optional": False,
    },
    {"question": "Anything specific to add?", "answer": "Worked great.", "options": None, "optional": True},
]


def _seed_row(answers: list[dict]) -> None:
    with get_connection() as conn:
        ensure_schema(conn)
        insert_feedback(conn, settings.deployment_id, answers)


def test_dashboard_page_rejects_missing_token():
    response = client.get("/")
    assert response.status_code == 403


def test_dashboard_page_rejects_wrong_token():
    response = client.get("/", params={"token": "not-the-right-token"})
    assert response.status_code == 403


def test_dashboard_page_serves_html_with_correct_token():
    response = client.get("/", params={"token": settings.dashboard_token})
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Feedback dashboard" in response.text


def test_dashboard_feedback_api_rejects_missing_or_wrong_token():
    assert client.get("/api/dashboard/feedback").status_code == 403
    assert client.get("/api/dashboard/feedback", params={"token": "wrong"}).status_code == 403


def test_dashboard_feedback_api_returns_rows_and_aggregated_summary():
    _seed_row([SUBMISSION[0], SUBMISSION[2]])  # "Yes" + a comment
    _seed_row([SUBMISSION[1]])  # "No", no comment

    response = client.get("/api/dashboard/feedback", params={"token": settings.dashboard_token})
    assert response.status_code == 200
    body = response.json()

    assert len(body["rows"]) >= 2  # other tests in this session may add more; just needs to include ours
    assert all("submitted_at" in row for row in body["rows"])

    by_question = {s["question"]: s for s in body["summary"]}
    understood = by_question["Did the agent understand your request correctly?"]
    assert understood["counts"]["Yes"] >= 1
    assert understood["counts"]["No"] >= 1

    comments = by_question["Anything specific to add?"]
    assert "Worked great." in comments["text_answers"]


def test_export_rejects_missing_or_wrong_token():
    assert client.get("/api/dashboard/export").status_code == 403
    assert client.get("/api/dashboard/export", params={"token": "wrong"}).status_code == 403


def test_export_csv_has_one_column_per_question_across_questionnaire_versions():
    # Unique question text so rows from other tests in this shared DB don't matter.
    tag = uuid.uuid4().hex[:8]
    q_old, q_new = f"Old question {tag}", f"New question {tag}"
    _seed_row([{"question": q_old, "answer": "Yes", "options": ["Yes", "No"], "optional": False}])
    _seed_row([{"question": q_new, "answer": "Great, thanks", "options": None, "optional": True}])

    response = client.get("/api/dashboard/export", params={"token": settings.dashboard_token, "format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["content-disposition"].startswith("attachment;")

    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {"submitted_at", q_old, q_new} <= set(rows[0].keys())
    old_row = next(r for r in rows if r[q_old] == "Yes")
    new_row = next(r for r in rows if r[q_new] == "Great, thanks")
    assert old_row[q_new] == "" and new_row[q_old] == ""  # each version's missing question is blank


def test_export_json_returns_the_raw_stored_records():
    tag = uuid.uuid4().hex[:8]
    _seed_row([{"question": f"JSON question {tag}", "answer": "No", "options": ["Yes", "No"], "optional": False}])

    response = client.get("/api/dashboard/export", params={"token": settings.dashboard_token, "format": "json"})
    assert response.status_code == 200
    records = json.loads(response.text)
    match = next(r for r in records if r["answers"][0]["question"] == f"JSON question {tag}")
    assert match["answers"][0]["answer"] == "No"
    assert "submitted_at" in match
