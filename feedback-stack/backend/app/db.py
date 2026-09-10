import psycopg
from psycopg.types.json import Jsonb

from app.core.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    answers JSONB NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def get_connection() -> psycopg.Connection:
    return psycopg.connect(settings.database_url)


def ensure_schema(conn: psycopg.Connection) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def insert_feedback(conn: psycopg.Connection, deployment_id: str, answers: list[dict]) -> None:
    """`answers` is the full submitted questionnaire (question text + answer
    + options per entry, not just bare values) so each row stays
    self-describing even after the questionnaire is edited or replaced."""
    conn.execute(
        "INSERT INTO feedback (deployment_id, answers) VALUES (%s, %s)",
        (deployment_id, Jsonb(answers)),
    )
    conn.commit()


def list_feedback(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute("SELECT id, answers, submitted_at FROM feedback ORDER BY submitted_at DESC").fetchall()
    return [{"id": row_id, "answers": answers, "submitted_at": submitted_at.isoformat()} for row_id, answers, submitted_at in rows]
