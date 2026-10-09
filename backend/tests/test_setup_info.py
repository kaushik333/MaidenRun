import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.aws.ec2 import INSTANCE_ROLE_NAME
from app.main import app

client = TestClient(app)

INFRA_DIR = Path(__file__).resolve().parents[2] / "infra"


def test_setup_info_matches_the_actual_infra_files_exactly() -> None:
    response = client.get("/setup-info")
    assert response.status_code == 200
    body = response.json()

    assert body["iam_policy"] == json.loads((INFRA_DIR / "iam-policy.json").read_text())
    assert body["instance_role_policy"] == json.loads((INFRA_DIR / "instance-role-policy.json").read_text())
    assert body["instance_role_name"] == INSTANCE_ROLE_NAME == "maidenrun-instance-role"
