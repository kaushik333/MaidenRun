import json
import stat
from pathlib import Path

import boto3

from app.core.config import settings
from app.core.logging import logger


class NoCredentialsError(Exception):
    """Raised when no AWS credentials are cached and none were provided."""


def _credentials_path() -> Path:
    return Path(settings.aws_credentials_path).expanduser()


def save_credentials(access_key_id: str, secret_access_key: str) -> None:
    """Caches AWS credentials in a local file readable only by the current user."""
    path = _credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"access_key_id": access_key_id, "secret_access_key": secret_access_key}))
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    logger.info("cached AWS credentials at %s", path)


def load_credentials() -> tuple[str, str] | None:
    """Returns (access_key_id, secret_access_key) from the local cache, or None."""
    path = _credentials_path()
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return data["access_key_id"], data["secret_access_key"]


def get_session(
    access_key_id: str | None = None,
    secret_access_key: str | None = None,
    region: str | None = None,
) -> boto3.Session:
    """Returns a session scoped to whatever IAM policy is attached to the key.

    Pass access_key_id/secret_access_key the first time (e.g. from the
    deploy form) to cache them locally; every later call can omit both and
    reuse what's cached instead, so actions after the first deploy never
    need to re-prompt for credentials. `region` is per-call, not cached —
    it's the region to deploy *into* this time (the user's own choice on
    the form), not a property of the credentials themselves.
    """
    if access_key_id and secret_access_key:
        save_credentials(access_key_id, secret_access_key)
    else:
        cached = load_credentials()
        if cached is None:
            raise NoCredentialsError(
                "No AWS credentials cached yet and none were provided. "
                "Paste an access key at least once (see infra/README.md)."
            )
        access_key_id, secret_access_key = cached

    return boto3.Session(
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region or settings.aws_default_region,
    )
