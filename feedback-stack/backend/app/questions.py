import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError

from app.core.logging import logger


class _QuestionsYamlLoader(yaml.SafeLoader):
    """PyYAML's default loader follows YAML 1.1, which resolves unquoted
    Yes/No/On/Off (in any case) to booleans — exactly the words a multiple-
    choice question's options are likely to use verbatim, as IDEA.md's own
    `options: [Yes, Partially, No]` example does. Restrict the bool
    resolver to YAML 1.2's narrower true/false so those stay plain strings,
    while `optional: true` still parses as a real boolean.
    """


_QuestionsYamlLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in mappings if tag != "tag:yaml.org,2002:bool"]
    for key, mappings in _QuestionsYamlLoader.yaml_implicit_resolvers.items()
}
_QuestionsYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


class Question(BaseModel):
    id: str
    type: Literal["multiple_choice", "scale", "open_text"]
    question: str
    options: list[str] | None = None
    range: tuple[int, int] | None = None
    optional: bool = False


# The four dimensions + open text from IDEA.md's design doc: understanding,
# accuracy, error-free completion, and would-use-again, plus one free-form
# field any domain expert can answer without technical knowledge.
DEFAULT_QUESTIONS: list[Question] = [
    Question(
        id="understood_request",
        type="multiple_choice",
        question="Did the agent understand your request correctly?",
        options=["Yes", "Partially", "No"],
    ),
    Question(
        id="accuracy",
        type="scale",
        question="How would you rate the accuracy of the response?",
        range=(1, 5),
    ),
    Question(
        id="completed_without_errors",
        type="multiple_choice",
        question="Did the task complete without errors?",
        options=["Yes", "No"],
    ),
    Question(
        id="would_use_again",
        type="multiple_choice",
        question="Would you use this agent again?",
        options=["Yes", "No"],
    ),
    Question(
        id="comments",
        type="open_text",
        question="Anything specific to add?",
        optional=True,
    ),
]


def parse_questions_yaml(text: str) -> list[Question]:
    """Parses a feedback-questions.yaml document into Questions.

    Schema is parsed, not interpreted — question *content* (wording,
    option choices) is never validated or judged, only its shape. Raises
    yaml.YAMLError on malformed YAML or pydantic.ValidationError on a
    schema that doesn't match Question — callers decide how to handle that
    (load_questions falls back to the default questionnaire).
    """
    parsed = yaml.load(text, Loader=_QuestionsYamlLoader) or {}
    questions = parsed.get("questions") or []
    return [Question(id=f"q{i}", **q) for i, q in enumerate(questions)]


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
        questions = parse_questions_yaml(text)
    except (yaml.YAMLError, ValidationError) as exc:
        logger.warning("could not parse %s, falling back to the default questionnaire: %s", path, exc)
        return DEFAULT_QUESTIONS

    return questions or DEFAULT_QUESTIONS


def validate_answers(questions: list[Question], answers: dict[str, str | int]) -> list[str]:
    """Structural validation only — required-answered, choice-in-options,
    scale-in-range. Never judges whether an answer makes sense."""
    errors = []
    for q in questions:
        value = answers.get(q.id)
        if value is None or value == "":
            if not q.optional:
                errors.append(f"{q.id}: an answer is required")
            continue

        if q.type == "multiple_choice" and value not in (q.options or []):
            errors.append(f"{q.id}: '{value}' is not one of {q.options}")
        elif q.type == "scale":
            lo, hi = q.range or (1, 5)
            if not isinstance(value, int) or not (lo <= value <= hi):
                errors.append(f"{q.id}: must be an integer between {lo} and {hi}")

    return errors
