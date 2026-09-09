import json
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

from app.aws.ec2 import INSTANCE_ROLE_NAME

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
