import psycopg
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.questions import DEFAULT_QUESTIONS


def _valid_answers() -> list[dict]:
    """A fully, validly answered submission matching the app's actual
    default questionnaire — built from DEFAULT_QUESTIONS rather than
    hardcoded, so it can't silently drift out of sync with it."""
    return [
        {**q.model_dump(), "answer": (q.options[0] if q.options else "Worked great.")} for q in DEFAULT_QUESTIONS
    ]


def test_get_questions_returns_default_questionnaire():
    with TestClient(app) as client:
        response = client.get("/api/questions")
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert [q["question"] for q in body["questions"]] == [q.question for q in DEFAULT_QUESTIONS]
    assert all(q["answer"] == "" for q in body["questions"])


def test_submit_feedback_writes_row_to_postgres_with_correct_shape():
    answers = _valid_answers()
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": answers})
    assert response.status_code == 201
    assert response.json() == {"status": "recorded"}

    conn = psycopg.connect(settings.database_url)
    try:
        row = conn.execute(
            "SELECT deployment_id, answers, submitted_at FROM feedback ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    deployment_id, stored_answers, submitted_at = row
    assert deployment_id == settings.deployment_id
    # The full question+answer+options shape is preserved, not just bare
    # values — a row stays self-describing even if the questionnaire
    # changes later.
    assert stored_answers == answers
    assert submitted_at is not None


def test_submit_feedback_rejects_missing_required_answer():
    answers = _valid_answers()
    answers[0]["answer"] = ""  # first default question is required
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": answers})
    assert response.status_code == 422
    assert "answer is required" in response.json()["detail"]


def test_submit_feedback_rejects_value_not_in_declared_options():
    answers = _valid_answers()
    answers[0]["answer"] = "Maybe"  # not one of the declared options
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": answers})
    assert response.status_code == 422
    assert "not one of" in response.json()["detail"]


def test_submit_feedback_allows_optional_question_left_blank():
    answers = _valid_answers()
    answers[-1]["answer"] = ""  # "comments" is the optional trailing question
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": answers})
    assert response.status_code == 201
