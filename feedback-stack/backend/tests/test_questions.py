from pathlib import Path

from app.questions import DEFAULT_QUESTIONS, load_questions, parse_questions_yaml, validate_answers

FIXTURE = Path(__file__).parent / "fixtures" / "feedback-questions.yaml"


def test_load_questions_returns_default_when_file_missing(tmp_path):
    questions = load_questions(tmp_path / "does-not-exist.yaml")
    assert questions == DEFAULT_QUESTIONS


def test_load_questions_returns_default_when_file_empty(tmp_path):
    path = tmp_path / "feedback-questions.yaml"
    path.write_text("")
    assert load_questions(path) == DEFAULT_QUESTIONS


def test_load_questions_parses_custom_yaml_fixture():
    questions = load_questions(FIXTURE)

    assert questions != DEFAULT_QUESTIONS
    assert len(questions) == 3
    assert questions[0].type == "multiple_choice"
    assert questions[0].options == ["Yes", "Partially", "No"]
    assert questions[1].type == "scale"
    assert questions[1].range == (1, 5)
    assert questions[2].type == "open_text"
    assert questions[2].optional is True


def test_load_questions_falls_back_on_malformed_yaml(tmp_path):
    path = tmp_path / "feedback-questions.yaml"
    path.write_text("questions:\n  - type: not_a_real_type\n    question: bad\n")
    assert load_questions(path) == DEFAULT_QUESTIONS


def test_parse_questions_yaml_assigns_positional_ids():
    questions = parse_questions_yaml(FIXTURE.read_text())
    assert [q.id for q in questions] == ["q0", "q1", "q2"]


def test_validate_answers_requires_non_optional_questions():
    errors = validate_answers(DEFAULT_QUESTIONS, {})
    required_ids = {q.id for q in DEFAULT_QUESTIONS if not q.optional}
    assert len(errors) == len(required_ids)


def test_validate_answers_accepts_only_declared_options():
    answers = {
        "understood_request": "Maybe",  # not one of Yes/Partially/No
        "accuracy": 4,
        "completed_without_errors": "Yes",
        "would_use_again": "Yes",
    }
    errors = validate_answers(DEFAULT_QUESTIONS, answers)
    assert any("understood_request" in e for e in errors)


def test_validate_answers_rejects_scale_out_of_range():
    answers = {
        "understood_request": "Yes",
        "accuracy": 9,
        "completed_without_errors": "Yes",
        "would_use_again": "Yes",
    }
    errors = validate_answers(DEFAULT_QUESTIONS, answers)
    assert any("accuracy" in e for e in errors)


def test_validate_answers_allows_missing_optional_question():
    answers = {
        "understood_request": "Yes",
        "accuracy": 5,
        "completed_without_errors": "Yes",
        "would_use_again": "Yes",
        # "comments" (optional) intentionally omitted
    }
    assert validate_answers(DEFAULT_QUESTIONS, answers) == []
