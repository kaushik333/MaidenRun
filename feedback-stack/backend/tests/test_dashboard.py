from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dashboard import router as dashboard_router
from app.core.config import settings
from app.db import get_connection, insert_feedback

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
        insert_feedback(conn, settings.deployment_id, answers, questionnaire_version=1)


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

    assert len(body["rows"]) == 2
    assert all("submitted_at" in row for row in body["rows"])

    by_question = {s["question"]: s for s in body["summary"]}
    understood = by_question["Did the agent understand your request correctly?"]
    assert understood["counts"]["Yes"] >= 1
    assert understood["counts"]["No"] >= 1

    comments = by_question["Anything specific to add?"]
    assert "Worked great." in comments["text_answers"]

