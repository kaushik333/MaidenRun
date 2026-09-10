import io
import os
import tarfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from app.core.logging import logger

BUCKET_NAME_PREFIX = "agent-feedback-deploy"
BUNDLE_KEY_PREFIX = "feedback-stack-bundles/"
LIFECYCLE_EXPIRATION_DAYS = 1

# Same Docker-vs-local-checkout path-depth split as setup_info.py's
# INFRA_DIR — the backend Dockerfile sets this explicitly since collapsing
# WORKDIR /app in place of the repo's backend/ folder shifts this file by
# one level.
FEEDBACK_STACK_DIR = Path(
    os.environ.get("FEEDBACK_STACK_DIR", str(Path(__file__).resolve().parents[3] / "feedback-stack"))
)

# Skipped when building the bundle — local dev/test artifacts and anything
# not needed to `docker compose up -d` the shipped code (matches .gitignore
# plus a few directories genuinely only useful in this repo, not on the
# instance). Belt-and-suspenders: the backend Dockerfile's own narrow COPY
# list already excludes most of these from FEEDBACK_STACK_DIR in production,
# this also covers a local (non-Docker) run of the control plane.
EXCLUDED_DIR_NAMES = {".venv", "node_modules", "dist", "__pycache__", ".pytest_cache", "tests", "fixtures"}
EXCLUDED_FILE_NAMES = {".env", ".env.example", "feedback-questions.example.json", "uv.lock"}


def ensure_bundle_bucket(session: boto3.Session) -> str:
    """Creates (or reuses) the S3 bucket this account's feedback-stack
    bundles are relayed through. Bucket names are globally unique across all
    AWS accounts, so the account id is baked into the name.
    """
    account_id = session.client("sts").get_caller_identity()["Account"]
    bucket_name = f"{BUCKET_NAME_PREFIX}-{account_id}"
    s3 = session.client("s3")

    try:
        s3.head_bucket(Bucket=bucket_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "404":
            raise
        region = session.region_name
        if region == "us-east-1":
            # S3's one API inconsistency: passing a LocationConstraint for
            # us-east-1 itself is rejected, every other region requires one.
            s3.create_bucket(Bucket=bucket_name)
        else:
            s3.create_bucket(Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": region})
        logger.info("created S3 bucket %s for feedback-stack bundles", bucket_name)

    # Safety net for a bundle whose explicit delete (post-health-check, or on
    # a failed/rolled-back deploy) never ran — e.g. the control plane crashed
    # mid-deploy. Idempotent to re-apply, so no need to track whether this
    # bucket already has the rule.
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-feedback-stack-bundles",
                    "Filter": {"Prefix": BUNDLE_KEY_PREFIX},
                    "Status": "Enabled",
                    "Expiration": {"Days": LIFECYCLE_EXPIRATION_DAYS},
                }
            ]
        },
    )
    return bucket_name


def build_bundle(questionnaire_json: str) -> bytes:
    """Tars feedback-stack/'s own source plus the uploaded questionnaire
    (written in as feedback-questions.json at the bundle root, alongside
    docker-compose.yml) into an in-memory gzipped tarball."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path in FEEDBACK_STACK_DIR.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(FEEDBACK_STACK_DIR)
            if any(part in EXCLUDED_DIR_NAMES for part in relative.parts):
                continue
            if relative.name in EXCLUDED_FILE_NAMES:
                continue
            tar.add(path, arcname=str(relative))

        questions_bytes = questionnaire_json.encode("utf-8")
        info = tarfile.TarInfo(name="feedback-questions.json")
        info.size = len(questions_bytes)
        tar.addfile(info, io.BytesIO(questions_bytes))

    return buffer.getvalue()


def upload_bundle(session: boto3.Session, bucket: str, key: str, data: bytes) -> None:
    session.client("s3").put_object(Bucket=bucket, Key=key, Body=data)
    logger.info("uploaded feedback-stack bundle to s3://%s/%s (%d bytes)", bucket, key, len(data))


def delete_bundle(session: boto3.Session, bucket: str, key: str) -> None:
    """Best-effort cleanup — a failure here is logged, not raised: the
    bucket's lifecycle rule (see ensure_bundle_bucket) will still expire the
    object later even if this delete itself fails."""
    try:
        session.client("s3").delete_object(Bucket=bucket, Key=key)
        logger.info("deleted feedback-stack bundle s3://%s/%s", bucket, key)
    except ClientError:
        logger.exception("could not delete feedback-stack bundle s3://%s/%s", bucket, key)
