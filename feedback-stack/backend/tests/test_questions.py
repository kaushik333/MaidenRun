from pathlib import Path

from app.questions import DEFAULT_QUESTIONS, load_questions, parse_questions_json, validate_answers

FIXTURE = Path(__file__).parent / "fixtures" / "feedback-questions.json"


def test_load_questions_returns_default_when_file_missing(tmp_path):
    questions = load_questions(tmp_path / "does-not-exist.json")
    assert questions == DEFAULT_QUESTIONS


def test_load_questions_returns_default_when_file_empty(tmp_path):
    path = tmp_path / "feedback-questions.json"
    path.write_text("")
    assert load_questions(path) == DEFAULT_QUESTIONS


def test_load_questions_parses_custom_json_fixture():
    questions = load_questions(FIXTURE)

    assert questions != DEFAULT_QUESTIONS
    assert len(questions) == 3
    assert questions[0].question == "Did the agent understand your request correctly?"
    assert questions[0].options == ["Yes", "Partially", "No"]
    assert questions[0].optional is False
    assert questions[1].options == ["1", "2", "3", "4", "5"]
    assert questions[2].options is None
    assert questions[2].optional is True


def test_load_questions_falls_back_on_malformed_json(tmp_path):
    path = tmp_path / "feedback-questions.json"
    path.write_text("{not valid json")
    assert load_questions(path) == DEFAULT_QUESTIONS


def test_load_questions_falls_back_when_schema_does_not_match(tmp_path):
    path = tmp_path / "feedback-questions.json"
    path.write_text('{"questions": [{"no_question_field": true}]}')
    assert load_questions(path) == DEFAULT_QUESTIONS


def test_parse_questions_json_leaves_answer_blank_by_default():
    questions = parse_questions_json(FIXTURE.read_text())
    assert all(q.answer == "" for q in questions)


def test_validate_answers_requires_non_optional_questions():
    unanswered = [q.model_copy() for q in DEFAULT_QUESTIONS]
    errors = validate_answers(unanswered)
    required_count = sum(1 for q in DEFAULT_QUESTIONS if not q.optional)
    assert len(errors) == required_count


def test_validate_answers_accepts_only_declared_options():
    submitted = [q.model_copy(update={"answer": "Yes"}) for q in DEFAULT_QUESTIONS]
    submitted[0] = submitted[0].model_copy(update={"answer": "Maybe"})  # not one of Yes/Partially/No
    submitted[1] = submitted[1].model_copy(update={"answer": "4"})
    errors = validate_answers(submitted)
    assert any("understand your request" in e for e in errors)


def test_validate_answers_rejects_option_not_in_declared_list():
    submitted = [q.model_copy(update={"answer": "Yes"}) for q in DEFAULT_QUESTIONS]
    submitted[1] = submitted[1].model_copy(update={"answer": "9"})  # not one of 1-5
    errors = validate_answers(submitted)
    assert any("accuracy" in e.lower() or "rate" in e.lower() for e in errors)


def test_validate_answers_allows_missing_optional_question():
    submitted = [q.model_copy(update={"answer": q.options[0] if q.options else ""}) for q in DEFAULT_QUESTIONS]
    # the last DEFAULT_QUESTIONS entry ("comments") is optional and left blank
    assert validate_answers(submitted) == []
