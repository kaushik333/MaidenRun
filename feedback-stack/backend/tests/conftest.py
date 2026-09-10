import os
import subprocess
import time

CONTAINER_NAME = "feedback-stack-test-postgres"
TEST_DB_PORT = 55432
TEST_DB_PASSWORD = "test"
DATABASE_URL = f"postgresql://postgres:{TEST_DB_PASSWORD}@localhost:{TEST_DB_PORT}/feedback"

# Set before any test module (or app.core.config) is imported — pytest_configure
# runs ahead of collection, which is the only way env-var-at-import-time
# settings can see this in time.
os.environ["DATABASE_URL"] = DATABASE_URL
os.environ.setdefault("DEPLOYMENT_ID", "test-deployment")


def _wait_until_ready(deadline: float) -> None:
    import psycopg

    last_error = None
    while time.monotonic() < deadline:
        try:
            psycopg.connect(DATABASE_URL, connect_timeout=2).close()
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"test postgres never became ready: {last_error}")


def pytest_configure(config) -> None:
    """Spins up a real, throwaway postgres:16 container for the whole test
    session — the exit test in plan.md's Phase 7 explicitly wants feedback
    rows verified against real Postgres, not a stand-in that wouldn't catch
    a real SQL/JSONB mistake."""
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
    subprocess.run(
        [
            "docker", "run", "-d", "--name", CONTAINER_NAME,
            "-e", f"POSTGRES_PASSWORD={TEST_DB_PASSWORD}",
            "-e", "POSTGRES_DB=feedback",
            "-p", f"{TEST_DB_PORT}:5432",
            "postgres:16",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _wait_until_ready(deadline=time.monotonic() + 30)


def pytest_unconfigure(config) -> None:
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True)
