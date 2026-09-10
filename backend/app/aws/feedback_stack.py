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


def render_feedback_user_data(deployment_id: str, region: str, bucket: str, key: str) -> str:
    """Boot script: install Docker, pull the feedback-stack code + uploaded
    questionnaire bundle from S3, and bring it up via its own
    docker-compose.yml.

    The bundle is fetched via `aws s3 cp`, authenticated by this instance's
    own IAM role (already used to read the Postgres password from SSM below)
    — no git clone, no image registry, no SSH. Retries a few times before
    giving up: covers both a transient S3 blip and the control plane not
    having finished the upload yet in some unexpected ordering.

    Postgres gets no host-side port at all — it's reachable only by the
    other two containers, over docker-compose's own network, by container
    name. Its password is fetched from SSM the same way the app instance
    fetches its LLM key, never appearing here as plaintext — docker compose
    picks both it and DEPLOYMENT_ID up automatically from this script's own
    exported shell environment.
    """
    password_export = (
        f'export POSTGRES_PASSWORD="$(aws ssm get-parameter '
        f'--name "{PARAMETER_NAMESPACE}/{deployment_id}/POSTGRES_PASSWORD" '
        f'--with-decryption --query Parameter.Value --output text --region {region})"'
    )
    pull_with_retry = (
        f"for i in 1 2 3 4 5; do "
        f"aws s3 cp s3://{bucket}/{key} /opt/feedback-bundle.tar.gz --region {region} && break || sleep 10; "
        f"done"
    )

    return (
        "#!/bin/bash\n"
        "set -euo pipefail\n\n"
        f"{DOCKER_INSTALL}\n\n"
        f"{password_export}\n"
        f'export DEPLOYMENT_ID="{deployment_id}"\n\n'
        f"{pull_with_retry}\n"
        "mkdir -p /opt/feedback-stack\n"
        "tar -xzf /opt/feedback-bundle.tar.gz -C /opt/feedback-stack\n"
        "cd /opt/feedback-stack\n"
        "docker compose up -d\n"
    )


def launch_feedback_stack(
    session: boto3.Session,
    deployment_id: str,
    bucket: str,
    key: str,
    instance_name: str | None = None,
) -> dict:
    """Provisions the feedback-stack EC2 instance.

    Returns {"instance_id", "public_ip", "dashboard_token"}. Same "returns
    once it has a public IP, not once the containers are actually serving"
    caveat as launch_app_instance — health is checked separately.

    dashboard_token is generated here (used to gate :8003 once a later phase
    builds the real dashboard) but nothing persists it server-side yet —
    there's no metadata table to put it in, since nothing can write to the
    just-launched Postgres from the control plane (no host port, reachable
    only from the instance's own Docker network). It's returned directly so
    the URL shape is already stable for that phase to build on.
    """
    postgres_password = secrets.token_urlsafe(24)
    write_secret_parameters(session, deployment_id, {"POSTGRES_PASSWORD": postgres_password})

    dashboard_token = secrets.token_urlsafe(32)
    security_group_id = ensure_feedback_security_group(session)
    ami_id, root_device_name = latest_al2023_ami(session)
    user_data = render_feedback_user_data(deployment_id=deployment_id, region=session.region_name, bucket=bucket, key=key)

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
