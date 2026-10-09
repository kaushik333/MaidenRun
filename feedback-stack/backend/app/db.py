from contextlib import AbstractContextManager

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.core.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    answers JSONB NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

# Pooled instead of a bare psycopg.connect() per request — each of uvicorn's
# --workers gets its own pool, so max_size here is a per-worker cap. Small on
# purpose: this workload is a handful of form submissions, not high-throughput
# traffic (see backend/Dockerfile for the worker count this is sized against).
pool = ConnectionPool(settings.database_url, min_size=1, max_size=5, open=True)


def get_connection() -> AbstractContextManager[psycopg.Connection]:
    return pool.connection()


def ensure_schema(conn: psycopg.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS is not race-free under real concurrency:
    with multiple uvicorn workers each running this at startup, two can both
    pass the existence check before either commits. The loser's error isn't
    consistently DuplicateTable — CREATE TABLE also creates a backing
    sequence and row type, so Postgres can instead report a UniqueViolation
    on whichever catalog entry collided first (observed in practice: the
    implicit SERIAL sequence's pg_class row). Either way it's harmless — the
    desired end state (table exists) is reached regardless of which worker
    "won"."""
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    except (psycopg.errors.DuplicateTable, psycopg.errors.UniqueViolation):
        conn.rollback()


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
    return [
        {"id": row_id, "answers": answers, "submitted_at": submitted_at.isoformat()}
        for row_id, answers, submitted_at in rows
    ]
