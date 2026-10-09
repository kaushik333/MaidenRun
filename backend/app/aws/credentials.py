import json
import os
import stat
from pathlib import Path

import boto3
from botocore.exceptions import ProfileNotFound, SSOTokenLoadError, TokenRetrievalError, UnauthorizedSSOTokenError

from app.core.config import settings
from app.core.logging import logger

# The control plane runs on the user's own machine, never on EC2, so skip the
# credential chain's EC2-metadata lookup — otherwise every call without a
# profile would first wait out a ~1s network timeout.
os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")


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
    """Returns a session for the first credentials found, in this order:

    1. A key pasted in this request — an explicit choice, so it always wins
       (and is cached locally for later calls, as before).
    2. The standard AWS credential chain — AWS_PROFILE / the default profile
       in ~/.aws (incl. SSO) or AWS_* env vars — same as the AWS CLI, Terraform
       and every other AWS tool.
    3. A key pasted on an earlier deploy and cached locally.

    `region` is per-call, not part of the credentials — it's the region the
    user chose to deploy into this time.
    """
    region = region or settings.aws_default_region
    if access_key_id and secret_access_key:
        save_credentials(access_key_id, secret_access_key)
        return boto3.Session(
            aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key, region_name=region
        )

    profile = settings.aws_profile or None
    try:
        chain = boto3.Session(profile_name=profile, region_name=region)
        if chain.get_credentials() is not None:
            return chain
    except ProfileNotFound as exc:
        raise NoCredentialsError(
            f"AWS profile {profile!r} (AWS_PROFILE) wasn't found — check ~/.aws/config, or AWS_CONFIG_DIR in .env "
            "when running under docker compose."
        ) from exc
    except (SSOTokenLoadError, UnauthorizedSSOTokenError, TokenRetrievalError) as exc:
        raise NoCredentialsError(
            f"Your AWS SSO session has expired — run `aws sso login --profile {profile or 'default'}` and retry."
        ) from exc

    cached = load_credentials()
    if cached is None:
        raise NoCredentialsError(
            "No AWS credentials cached yet and none were provided. Set up an AWS profile (AWS_PROFILE) "
            "or paste an access key (see infra/README.md)."
        )
    return boto3.Session(aws_access_key_id=cached[0], aws_secret_access_key=cached[1], region_name=region)


def credential_source(session: boto3.Session) -> str:
    """Human-readable description of where a session's credentials came from."""
    method = session.get_credentials().method
    if method == "explicit":
        return "saved access key"
    if method == "env":
        return "AWS environment variables"
    if method == "sso":
        return f"AWS SSO profile '{session.profile_name}'"
    return f"AWS profile '{session.profile_name}'"
