import json
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.core.logging import logger


class Question(BaseModel):
    """The one shape used for both the questionnaire schema and a
    submission: `GET /api/questions` returns these with `answer` always
    blank, the frontend fills `answer` in per question, and `POST
    /api/feedback` gets the same array back. There's no separate `type`
    field — the presence of `options` is what makes a question render (and
    validate) as a set of choices instead of a free-text box.
    """

    question: str
    answer: str = ""
    options: list[str] | None = None
    optional: bool = False


# The four dimensions + open text from IDEA.md's design doc: understanding,
# accuracy, error-free completion, and would-use-again, plus one free-form
# field any domain expert can answer without technical knowledge.
DEFAULT_QUESTIONS: list[Question] = [
    Question(
        question="Did the agent understand your request correctly?",
        options=["Yes", "Partially", "No"],
    ),
    Question(
        question="How would you rate the accuracy of the response?",
        options=["1", "2", "3", "4", "5"],
    ),
    Question(
        question="Did the task complete without errors?",
        options=["Yes", "No"],
    ),
    Question(
        question="Would you use this agent again?",
        options=["Yes", "No"],
    ),
    Question(
        question="Anything specific to add?",
        optional=True,
    ),
]


def parse_questions_json(text: str) -> list[Question]:
    """Parses a feedback-questions.json document into Questions.

    Schema is parsed, not interpreted — question *content* (wording,
    option choices) is never validated or judged, only its shape. Raises
    json.JSONDecodeError on malformed JSON or pydantic.ValidationError on a
    schema that doesn't match Question — callers decide how to handle that
    (load_questions falls back to the default questionnaire).
    """
    parsed = json.loads(text)
    questions = parsed.get("questions") or []
    return [Question(**q) for q in questions]


def parse_questionnaire_upload(text: str) -> list[Question]:
    """Strict parsing for a new questionnaire version uploaded from the control
    plane: same JSON format as the file bundled at deploy time, but unlike
    load_questions there's no silent fallback — the uploader gets told exactly
    what's wrong. Question text must be unique within a version, since each
    question becomes its own column in exports."""
    try:
        questions = parse_questions_json(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Not valid JSON: {exc}") from exc
    except (ValidationError, AttributeError, TypeError) as exc:
        raise ValueError(f"Doesn't match the questionnaire format ({{\"questions\": [...]}}): {exc}") from exc
    if not questions:
        raise ValueError('The questionnaire has no questions — expected {"questions": [...]} with at least one.')
    seen, duplicates = set(), set()
    for q in questions:
        (duplicates if q.question in seen else seen).add(q.question)
    if duplicates:
        raise ValueError(f"Each question must be unique within a version — repeated: {sorted(duplicates)}")
    return questions


def load_questions(path: Path) -> list[Question]:
    """Loads the questionnaire from `path` if it exists and is non-empty,
    otherwise falls back to DEFAULT_QUESTIONS. A malformed file also falls
    back to the default rather than crashing the service — a mistake in a
    user-supplied config file shouldn't take down feedback collection."""
    if not path.is_file():
        return DEFAULT_QUESTIONS

    text = path.read_text().strip()
    if not text:
        return DEFAULT_QUESTIONS

    try:
        questions = parse_questions_json(text)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("could not parse %s, falling back to the default questionnaire: %s", path, exc)
        return DEFAULT_QUESTIONS

    return questions or DEFAULT_QUESTIONS


def validate_answers(submitted: list[Question]) -> list[str]:
    """Structural validation only — required-answered, choice-in-options.
    Never judges whether an answer makes sense.

    Validates the submitted questions' own internal consistency (each
    question already carries its own `options`/`optional`, echoed back from
    what GET /api/questions handed out) rather than cross-referencing the
    server's separately-loaded schema — the submission is a complete,
    self-contained record, which is also exactly what gets stored.
    """
    errors = []
    for q in submitted:
        if not q.answer:
            if not q.optional:
                errors.append(f"{q.question!r}: an answer is required")
            continue

        if q.options and q.answer not in q.options:
            errors.append(f"{q.question!r}: {q.answer!r} is not one of {q.options}")

    return errors
