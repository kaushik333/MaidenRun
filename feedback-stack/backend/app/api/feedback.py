from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import logger
from app.db import get_connection, insert_feedback
from app.questions import Question, load_questions, validate_answers

router = APIRouter()

# Loaded once at import time — the questions file is written by the EC2 boot
# script before this container ever starts and never changes afterward.
QUESTIONS: list[Question] = load_questions(Path(settings.feedback_questions_path))


class FeedbackSubmission(BaseModel):
    answers: dict[str, str | int]


@router.get("/api/questions", response_model=list[Question])
def get_questions() -> list[Question]:
    return QUESTIONS


@router.post("/api/feedback", status_code=201)
def submit_feedback(submission: FeedbackSubmission) -> dict:
    errors = validate_answers(QUESTIONS, submission.answers)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    with get_connection() as conn:
        insert_feedback(conn, settings.deployment_id, submission.answers)

    logger.info("recorded feedback submission for deployment %s", settings.deployment_id)
    return {"status": "recorded"}
