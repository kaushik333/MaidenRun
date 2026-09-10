from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import logger
from app.db import get_connection, insert_feedback
from app.questions import Question, load_questions, validate_answers

router = APIRouter()

# Loaded once at import time — the questions file is bundled in before this
# container ever starts and never changes afterward.
QUESTIONS: list[Question] = load_questions(Path(settings.feedback_questions_path))


class FeedbackSubmission(BaseModel):
    answers: list[Question]


@router.get("/api/questions", response_model=list[Question])
def get_questions() -> list[Question]:
    # Blank out `answer` even if the loaded schema happened to have
    # something in it (e.g. a template's placeholder text) — a reviewer
    # should never see a pre-filled response that isn't theirs.
    return [q.model_copy(update={"answer": ""}) for q in QUESTIONS]


@router.post("/api/feedback", status_code=201)
def submit_feedback(submission: FeedbackSubmission) -> dict:
    errors = validate_answers(submission.answers)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    with get_connection() as conn:
        insert_feedback(conn, settings.deployment_id, [q.model_dump() for q in submission.answers])

    logger.info("recorded feedback submission for deployment %s", settings.deployment_id)
    return {"status": "recorded"}
