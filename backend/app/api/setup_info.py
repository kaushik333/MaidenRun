import json
import os
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.aws.credentials import NoCredentialsError, credential_source, get_session
from app.aws.ec2 import INSTANCE_ROLE_NAME, my_public_ip

# The Docker image sets INFRA_DIR explicitly (see backend/Dockerfile) since
# its directory depth relative to this file isn't the same as a local
# checkout's — collapsing WORKDIR /app in place of the repo's backend/
# folder shifts the file by one level. Local/host runs fall back to the
# actual repo-relative path.
INFRA_DIR = Path(os.environ.get("INFRA_DIR", str(Path(__file__).resolve().parents[3] / "infra")))

router = APIRouter()


class SetupInfo(BaseModel):
    iam_policy: dict
    instance_role_name: str
    instance_role_policy: dict


@router.get("/setup-info", response_model=SetupInfo)
def setup_info() -> SetupInfo:
    """Serves infra/*.json as data, so the in-app setup guide can never
    drift out of sync with what a user is actually told to paste into AWS."""
    return SetupInfo(
        iam_policy=json.loads((INFRA_DIR / "iam-policy.json").read_text()),
        instance_role_name=INSTANCE_ROLE_NAME,
        instance_role_policy=json.loads((INFRA_DIR / "instance-role-policy.json").read_text()),
    )


class MyIp(BaseModel):
    ip: str


@router.get("/my-ip", response_model=MyIp)
def my_ip() -> MyIp:
    """The public IP every IP allowlist automatically includes — shown in the
    deploy forms so the user knows they can't lock themselves out."""
    try:
        return MyIp(ip=my_public_ip())
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"Could not look up your public IP: {exc}") from exc


class AwsIdentity(BaseModel):
    available: bool
    source: str | None = None
    account: str | None = None
    arn: str | None = None
    error: str | None = None


@router.get("/aws-identity", response_model=AwsIdentity)
def aws_identity() -> AwsIdentity:
    """Which AWS identity deploys would use right now, if any — lets the
    forms hide the key fields when a profile or cached key is already set."""
    try:
        session = get_session()
        identity = session.client("sts").get_caller_identity()
    except NoCredentialsError as exc:
        return AwsIdentity(available=False, error=str(exc))
    except (ClientError, BotoCoreError) as exc:
        return AwsIdentity(available=False, error=f"AWS rejected these credentials: {exc}")
    return AwsIdentity(
        available=True, source=credential_source(session), account=identity["Account"], arn=identity["Arn"]
    )
