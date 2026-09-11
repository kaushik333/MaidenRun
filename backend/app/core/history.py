import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.logging import logger

# Durable, file-per-deployment record of every deployment this backend has
# ever completed — the source of truth behind the frontend's Deployments
# tab, and (via each router's own rehydrate_*_deployments()) what lets
# /status and /cost-estimate keep answering for old deployments across a
# backend restart, since the in-memory dicts in deploy.py/deploy_feedback.py
# are otherwise wiped on every process start. One JSON file per deployment
# rather than one shared file/database — this is a local, single-user
# control plane (see deploy.py's _deployments comment), so a database is
# real infrastructure weight for no real benefit; flat files are legible and
# need nothing new to run. Never pass AWS credentials, LLM api keys, or env
# var *values* into `record` — only their key names, if anything — since
# this directory is meant to persist indefinitely on disk.
_history_lock = threading.Lock()


def _history_dir() -> Path:
    path = Path(settings.deployments_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_path(deployment_id: str) -> Path:
    return _history_dir() / f"{deployment_id}.json"


def _write(path: Path, record: dict[str, Any]) -> None:
    # Write-to-temp-then-rename so a crash mid-write can never leave a
    # truncated/corrupt file for the next read (os.replace is atomic on the
    # same filesystem, which a bind-mounted directory always is here).
    tmp_path = path.parent / f"{path.name}.tmp"
    tmp_path.write_text(json.dumps(record, indent=2, default=str))
    os.replace(tmp_path, path)


def record_deployment(record: dict[str, Any]) -> None:
    path = _file_path(record["deployment_id"])
    with _history_lock:
        try:
            _write(path, record)
        except OSError:
            logger.exception("failed to write deployment history file %s", path)


def mark_terminated(deployment_id: str) -> None:
    path = _file_path(deployment_id)
    with _history_lock:
        try:
            if not path.exists():
                logger.warning("mark_terminated: no history file for deployment %s", deployment_id)
                return
            record = json.loads(path.read_text())
            record["status"] = "terminated"
            record["terminated_at"] = datetime.now(timezone.utc).isoformat()
            _write(path, record)
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to mark deployment %s terminated in history", deployment_id)


def list_deployments() -> list[dict[str, Any]]:
    records = []
    for file in _history_dir().glob("*.json"):
        try:
            records.append(json.loads(file.read_text()))
        except (OSError, json.JSONDecodeError):
            logger.exception("failed to read deployment history file %s", file)
    records.sort(key=lambda r: r.get("deployed_at", ""), reverse=True)
    return records
