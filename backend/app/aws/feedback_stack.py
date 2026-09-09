import secrets

import boto3

from app.aws.ec2 import (
    DOCKER_INSTALL,
    INSTANCE_ROLE_NAME,
    PARAMETER_NAMESPACE,
    get_default_vpc_id,
    latest_al2023_ami,
    wait_for_public_ip,
    write_secret_parameters,
)
from app.core.config import settings
from app.core.logging import logger

SECURITY_GROUP_NAME = "agent-feedback-deploy-feedback"

# Fixed, not user-configurable: a form + dashboard + Postgres don't need to
# scale with whatever the agent itself needs (see infra discussion — this
# mirrors the frontend's FEEDBACK_INSTANCE_TYPE/FEEDBACK_EBS_SIZE_GB).
INSTANCE_TYPE = "t3.small"
EBS_SIZE_GB = 20

PLACEHOLDER_HTML = (
    "<!doctype html><html><body style=\"font-family:sans-serif;text-align:center;margin-top:4rem\">"
    "<h1>Coming soon</h1><p>The feedback stack is up — the form and dashboard themselves ship in a "
    "later phase.</p></body></html>"
)


def ensure_feedback_security_group(session: boto3.Session) -> str:
    """Creates (or reuses) the security group that opens only 8002/8003 —
    never Postgres, which stays on the instance's internal Docker network."""
    ec2 = session.client("ec2")
    existing = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SECURITY_GROUP_NAME]}])[
        "SecurityGroups"
    ]
    if existing:
        return existing[0]["GroupId"]

    group_id = ec2.create_security_group(
        GroupName=SECURITY_GROUP_NAME,
        Description="Agent Feedback Deploy: feedback stack, ports 8002/8003 only",
        VpcId=get_default_vpc_id(ec2),
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": port,
                "ToPort": port,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
            for port in (settings.feedback_form_port, settings.feedback_dashboard_port)
        ],
    )
    return group_id


def render_feedback_user_data(deployment_id: str, region: str) -> str:
    """Boot script: install Docker, run Postgres + two placeholder pages.

    Postgres gets no host-side port at all — it's reachable only by the
    other two containers, over a private Docker network, by container name.
    The password is fetched from SSM the same way the app instance fetches
    its LLM key, never appearing here as plaintext.
    """
    password_export = (
        f'export POSTGRES_PASSWORD="$(aws ssm get-parameter '
        f'--name "{PARAMETER_NAMESPACE}/{deployment_id}/POSTGRES_PASSWORD" '
        f'--with-decryption --query Parameter.Value --output text --region {region})"'
    )

    return (
        "#!/bin/bash\n"
        "set -euo pipefail\n\n"
        f"{DOCKER_INSTALL}\n\n"
        f"{password_export}\n\n"
        "docker network create feedback-net\n"
        "docker volume create feedback-pgdata\n"
        "docker run -d --name postgres --network feedback-net --restart unless-stopped \\\n"
        '  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=feedback \\\n'
        "  -v feedback-pgdata:/var/lib/postgresql/data \\\n"
        "  postgres:16\n\n"
        "mkdir -p /opt/feedback-placeholder\n"
        f"cat > /opt/feedback-placeholder/index.html <<'HTML'\n{PLACEHOLDER_HTML}\nHTML\n\n"
        f"docker run -d --name feedback-form --network feedback-net --restart unless-stopped "
        f"-p {settings.feedback_form_port}:80 "
        "-v /opt/feedback-placeholder:/usr/share/nginx/html:ro nginx:alpine\n"
        f"docker run -d --name feedback-dashboard --network feedback-net --restart unless-stopped "
        f"-p {settings.feedback_dashboard_port}:80 "
        "-v /opt/feedback-placeholder:/usr/share/nginx/html:ro nginx:alpine\n"
    )


def launch_feedback_stack(session: boto3.Session, deployment_id: str, instance_name: str | None = None) -> dict:
    """Provisions the feedback-stack EC2 instance.

    Returns {"instance_id", "public_ip", "dashboard_token"}. Same "returns
    once it has a public IP, not once the containers are actually serving"
    caveat as launch_app_instance — health is checked separately.

    dashboard_token is generated here (used to gate :8003 once Phase 8
    builds the real dashboard) but nothing persists it server-side yet —
    Phase 6 has no metadata table to put it in, since nothing can write to
    the just-launched Postgres from the control plane (no host port,
    reachable only from the instance's own Docker network). It's returned
    directly in /deploy's response so the URL shape is already stable.
    """
    postgres_password = secrets.token_urlsafe(24)
    write_secret_parameters(session, deployment_id, {"POSTGRES_PASSWORD": postgres_password})

    dashboard_token = secrets.token_urlsafe(32)
    security_group_id = ensure_feedback_security_group(session)
    ami_id, root_device_name = latest_al2023_ami(session)
    user_data = render_feedback_user_data(deployment_id=deployment_id, region=session.region_name)

    ec2 = session.client("ec2")
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=INSTANCE_TYPE,
        MinCount=1,
        MaxCount=1,
        SecurityGroupIds=[security_group_id],
        IamInstanceProfile={"Name": INSTANCE_ROLE_NAME},
        UserData=user_data,
        BlockDeviceMappings=[{"DeviceName": root_device_name, "Ebs": {"VolumeSize": EBS_SIZE_GB, "VolumeType": "gp3"}}],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "deployment_id", "Value": deployment_id},
                    {"Key": "Name", "Value": instance_name or f"agent-feedback-deploy-{deployment_id}-feedback"},
                ],
            }
        ],
    )
    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("launched feedback stack instance %s for deployment %s", instance_id, deployment_id)

    public_ip = wait_for_public_ip(ec2, instance_id)
    return {"instance_id": instance_id, "public_ip": public_ip, "dashboard_token": dashboard_token}
