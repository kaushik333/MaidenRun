import psycopg
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app

VALID_ANSWERS = {
    "understood_request": "Yes",
    "accuracy": 4,
    "completed_without_errors": "Yes",
    "would_use_again": "Yes",
    "comments": "Worked great.",
}


def test_get_questions_returns_default_questionnaire():
    with TestClient(app) as client:
        response = client.get("/api/questions")
    assert response.status_code == 200
    ids = [q["id"] for q in response.json()]
    assert ids == ["understood_request", "accuracy", "completed_without_errors", "would_use_again", "comments"]


def test_submit_feedback_writes_row_to_postgres_with_correct_shape():
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": VALID_ANSWERS})
    assert response.status_code == 201
    assert response.json() == {"status": "recorded"}

    conn = psycopg.connect(settings.database_url)
    try:
        row = conn.execute(
            "SELECT deployment_id, answers, submitted_at FROM feedback ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    deployment_id, answers, submitted_at = row
    assert deployment_id == settings.deployment_id
    assert answers == VALID_ANSWERS
    assert submitted_at is not None


def test_submit_feedback_rejects_missing_required_answer():
    incomplete = {k: v for k, v in VALID_ANSWERS.items() if k != "accuracy"}
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": incomplete})
    assert response.status_code == 422
    assert "accuracy" in response.json()["detail"]


def test_submit_feedback_rejects_invalid_multiple_choice_value():
    bad = {**VALID_ANSWERS, "understood_request": "Maybe"}
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": bad})
    assert response.status_code == 422
    assert "understood_request" in response.json()["detail"]


def test_submit_feedback_rejects_out_of_range_scale():
    bad = {**VALID_ANSWERS, "accuracy": 10}
    with TestClient(app) as client:
        response = client.post("/api/feedback", json={"answers": bad})
    assert response.status_code == 422
    assert "accuracy" in response.json()["detail"]
