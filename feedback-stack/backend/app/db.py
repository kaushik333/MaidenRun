from contextlib import AbstractContextManager

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.core.config import settings

# Questionnaires and agent versions live here rather than in a file, so a new
# questionnaire version (or a newly deployed agent commit) takes effect at the
# same URL without redeploying anything. The partial unique indexes make
# "exactly one active version" a database guarantee, not just a convention.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    id SERIAL PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    answers JSONB NOT NULL,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS questionnaires (
    version SERIAL PRIMARY KEY,
    questions JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_questionnaire ON questionnaires (is_active) WHERE is_active;
CREATE TABLE IF NOT EXISTS agent_versions (
    id SERIAL PRIMARY KEY,
    commit_sha TEXT NOT NULL,
    commit_message TEXT NOT NULL DEFAULT '',
    repo_url TEXT NOT NULL DEFAULT '',
    activated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active BOOLEAN NOT NULL DEFAULT false
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_agent_version ON agent_versions (is_active) WHERE is_active;
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS questionnaire_version INT REFERENCES questionnaires (version);
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS agent_version_id INT REFERENCES agent_versions (id);
"""

# Any constant works — it only has to be the same for every process.
_SCHEMA_LOCK_ID = 727274

# Pooled instead of a bare psycopg.connect() per request — each of uvicorn's
# --workers gets its own pool, so max_size here is a per-worker cap. Small on
# purpose: this workload is a handful of form submissions, not high-throughput
# traffic (see backend/Dockerfile for the worker count this is sized against).
pool = ConnectionPool(settings.database_url, min_size=1, max_size=5, open=True)


def get_connection() -> AbstractContextManager[psycopg.Connection]:
    return pool.connection()


def ensure_schema(conn: psycopg.Connection, initial_questions: list[dict]) -> None:
    """Creates/upgrades the schema and, on first start, stores the questionnaire
    bundled at deploy time as version 1. Responses recorded before versioning
    existed are attributed to that version 1.

    Both containers (form + dashboard, several uvicorn workers each) run this at
    startup at the same moment; the transaction-scoped advisory lock makes them
    take turns, so the DDL and the one-time bootstrap never race.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
        conn.execute(_SCHEMA)
        if conn.execute("SELECT 1 FROM questionnaires LIMIT 1").fetchone() is None:
            conn.execute(
                "INSERT INTO questionnaires (version, questions, activated_at, is_active) "
                "VALUES (1, %s, now(), true)",
                (Jsonb(initial_questions),),
            )
            conn.execute("SELECT setval(pg_get_serial_sequence('questionnaires', 'version'), 1)")
        conn.execute("UPDATE feedback SET questionnaire_version = 1 WHERE questionnaire_version IS NULL")


# ---- questionnaires ----


def active_questionnaire(conn: psycopg.Connection) -> tuple[int, list[dict]]:
    version, questions = conn.execute("SELECT version, questions FROM questionnaires WHERE is_active").fetchone()
    return version, questions


def get_questionnaire(conn: psycopg.Connection, version: int) -> list[dict] | None:
    row = conn.execute("SELECT questions FROM questionnaires WHERE version = %s", (version,)).fetchone()
    return row[0] if row else None


def list_questionnaires(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT q.version, q.questions, q.created_at, q.activated_at, q.is_active, count(f.id)
        FROM questionnaires q LEFT JOIN feedback f ON f.questionnaire_version = q.version
        GROUP BY q.version ORDER BY q.version
        """
    ).fetchall()
    return [
        {
            "version": version,
            "questions": questions,
            "created_at": created_at.isoformat(),
            "activated_at": activated_at.isoformat() if activated_at else None,
            "is_active": is_active,
            "responses": responses,
        }
        for version, questions, created_at, activated_at, is_active, responses in rows
    ]


def create_questionnaire(conn: psycopg.Connection, questions: list[dict], activate: bool) -> int:
    with conn.transaction():
        (version,) = conn.execute(
            "INSERT INTO questionnaires (questions) VALUES (%s) RETURNING version", (Jsonb(questions),)
        ).fetchone()
        if activate:
            _activate_questionnaire(conn, version)
    return version


def activate_questionnaire(conn: psycopg.Connection, version: int) -> bool:
    """Makes `version` the one the form serves (also how a rollback works).
    Returns False if no such version exists."""
    with conn.transaction():
        if conn.execute("SELECT 1 FROM questionnaires WHERE version = %s", (version,)).fetchone() is None:
            return False
        _activate_questionnaire(conn, version)
    return True


def _activate_questionnaire(conn: psycopg.Connection, version: int) -> None:
    conn.execute("UPDATE questionnaires SET is_active = false WHERE is_active AND version <> %s", (version,))
    conn.execute(
        "UPDATE questionnaires SET is_active = true, activated_at = now() WHERE version = %s AND NOT is_active",
        (version,),
    )


# ---- agent versions ----


def set_agent_version(conn: psycopg.Connection, commit_sha: str, commit_message: str, repo_url: str) -> int:
    """Records which agent commit is live from now on; every later response is
    stamped with it. Re-announcing the commit that's already live is a no-op."""
    with conn.transaction():
        active = conn.execute("SELECT id, commit_sha FROM agent_versions WHERE is_active").fetchone()
        if active and active[1] == commit_sha:
            return active[0]
        conn.execute("UPDATE agent_versions SET is_active = false WHERE is_active")
        (agent_version_id,) = conn.execute(
            "INSERT INTO agent_versions (commit_sha, commit_message, repo_url, is_active) "
            "VALUES (%s, %s, %s, true) RETURNING id",
            (commit_sha, commit_message, repo_url),
        ).fetchone()
    return agent_version_id


def list_agent_versions(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.id, a.commit_sha, a.commit_message, a.repo_url, a.activated_at, a.is_active, count(f.id)
        FROM agent_versions a LEFT JOIN feedback f ON f.agent_version_id = a.id
        GROUP BY a.id ORDER BY a.id
        """
    ).fetchall()
    return [
        {
            "id": agent_version_id,
            "commit_sha": commit_sha,
            "commit_message": commit_message,
            "repo_url": repo_url,
            "activated_at": activated_at.isoformat(),
            "is_active": is_active,
            "responses": responses,
        }
        for agent_version_id, commit_sha, commit_message, repo_url, activated_at, is_active, responses in rows
    ]


# ---- responses ----


def insert_feedback(
    conn: psycopg.Connection, deployment_id: str, answers: list[dict], questionnaire_version: int
) -> None:
    """`answers` is the full submitted questionnaire (question text + answer +
    options per entry, not just bare values) so each row stays self-describing.
    The live agent version is stamped server-side at submission time."""
    conn.execute(
        "INSERT INTO feedback (deployment_id, answers, questionnaire_version, agent_version_id) "
        "VALUES (%s, %s, %s, (SELECT id FROM agent_versions WHERE is_active))",
        (deployment_id, Jsonb(answers), questionnaire_version),
    )
    conn.commit()


def list_feedback(conn: psycopg.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, answers, submitted_at, questionnaire_version, agent_version_id "
        "FROM feedback ORDER BY submitted_at DESC"
    ).fetchall()
    return [
        {
            "id": row_id,
            "answers": answers,
            "submitted_at": submitted_at.isoformat(),
            "questionnaire_version": questionnaire_version,
            "agent_version_id": agent_version_id,
        }
        for row_id, answers, submitted_at, questionnaire_version, agent_version_id in rows
    ]
