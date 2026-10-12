from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import settings
from app.core.logging import logger
from app.db import active_questionnaire, get_connection, get_questionnaire, insert_feedback
from app.questions import Question, validate_answers

router = APIRouter()


class ActiveQuestionnaire(BaseModel):
    version: int
    questions: list[Question]


class FeedbackSubmission(BaseModel):
    answers: list[Question]
    # The version the reviewer actually answered — a form opened before a new
    # version was published still records against the one it showed. Only a
    # pre-versioning form omits it (then: whatever is active now).
    questionnaire_version: int | None = None


@router.get("/api/questions", response_model=ActiveQuestionnaire)
def get_questions() -> ActiveQuestionnaire:
    # Read per request, not at startup — publishing a new questionnaire
    # version takes effect on the next page load, at the same URL.
    with get_connection() as conn:
        version, questions = active_questionnaire(conn)
    # Blank out `answer` even if the stored schema happened to have something
    # in it (e.g. a template's placeholder text) — a reviewer should never see
    # a pre-filled response that isn't theirs.
    return ActiveQuestionnaire(
        version=version, questions=[Question(**q).model_copy(update={"answer": ""}) for q in questions]
    )


@router.post("/api/feedback", status_code=201)
def submit_feedback(submission: FeedbackSubmission) -> dict:
    errors = validate_answers(submission.answers)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    with get_connection() as conn:
        version = submission.questionnaire_version or active_questionnaire(conn)[0]
        if get_questionnaire(conn, version) is None:
            raise HTTPException(status_code=422, detail=f"Unknown questionnaire version {version}")
        insert_feedback(conn, settings.deployment_id, [q.model_dump() for q in submission.answers], version)

    logger.info("recorded feedback (questionnaire v%s) for deployment %s", version, settings.deployment_id)
    return {"status": "recorded"}
