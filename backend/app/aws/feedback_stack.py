import secrets

import boto3

from app.aws.ec2 import (
    DOCKER_INSTALL,
    INSTANCE_ROLE_NAME,
    OPEN_CIDR,
    PARAMETER_NAMESPACE,
    create_security_group,
    latest_al2023_ami,
    wait_for_public_ip,
    write_secret_parameters,
)
from app.core.config import settings
from app.core.logging import logger

# Prefix of each feedback deployment's own security group: maidenrun-feedback-<deployment_id>
SECURITY_GROUP_NAME = "maidenrun-feedback"

# Fixed, not user-configurable: a form + dashboard + Postgres don't need to
# scale with whatever the agent itself needs (see infra discussion — this
# mirrors the frontend's FEEDBACK_INSTANCE_TYPE/FEEDBACK_EBS_SIZE_GB).
INSTANCE_TYPE = "t3.small"
EBS_SIZE_GB = 20


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
    def _ssm_export(name: str) -> str:
        return (
            f'export {name}="$(aws ssm get-parameter '
            f'--name "{PARAMETER_NAMESPACE}/{deployment_id}/{name}" '
            f'--with-decryption --query Parameter.Value --output text --region {region})"'
        )

    password_export = _ssm_export("POSTGRES_PASSWORD")
    # The dashboard needs to know its own token to check incoming requests
    # against — generated control-plane-side and handed to the user in the
    # URL, but the instance itself has no way to know it without this. Same
    # SSM SecureString route as the Postgres password, never appearing in
    # user-data in plaintext.
    dashboard_token_export = _ssm_export("DASHBOARD_TOKEN")
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
        f"{dashboard_token_export}\n"
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
    ingress: list[str] = (OPEN_CIDR,),
) -> dict:
    """Provisions the feedback-stack EC2 instance.

    Returns {"instance_id", "public_ip", "dashboard_token", "security_group_id"}. Same "returns
    once it has a public IP, not once the containers are actually serving"
    caveat as launch_app_instance — health is checked separately.

    dashboard_token is generated here and written to SSM (fetched by the
    instance's own boot script, same SecureString route as the Postgres
    password — see render_feedback_user_data) so the instance can actually
    check incoming :8003 requests against it, not just hand it back in the
    URL. Nothing persists it in the control plane itself — there's no
    metadata table, since nothing can write to the just-launched Postgres
    from here (no host port, reachable only from the instance's own Docker
    network) — so a lost/refreshed browser tab has no way to recover a
    dashboard link once it scrolls off screen.
    """
    postgres_password = secrets.token_urlsafe(24)
    dashboard_token = secrets.token_urlsafe(32)
    write_secret_parameters(
        session, deployment_id, {"POSTGRES_PASSWORD": postgres_password, "DASHBOARD_TOKEN": dashboard_token}
    )

    ami_id, root_device_name = latest_al2023_ami(session)
    user_data = render_feedback_user_data(
        deployment_id=deployment_id, region=session.region_name, bucket=bucket, key=key
    )

    # Only the form and dashboard — never Postgres, which stays on the
    # instance's internal Docker network.
    ports = [settings.feedback_form_port, settings.feedback_dashboard_port]
    security_group_id = create_security_group(
        session,
        name=f"{SECURITY_GROUP_NAME}-{deployment_id}",
        description=f"MaidenRun: feedback stack {deployment_id}, ports {ports[0]}/{ports[1]}",
        ports=ports,
        cidrs=list(ingress),
        deployment_id=deployment_id,
    )
    ec2 = session.client("ec2")
    try:
        response = ec2.run_instances(
            ImageId=ami_id,
            InstanceType=INSTANCE_TYPE,
            MinCount=1,
            MaxCount=1,
            SecurityGroupIds=[security_group_id],
            IamInstanceProfile={"Name": INSTANCE_ROLE_NAME},
            UserData=user_data,
            BlockDeviceMappings=[
                {"DeviceName": root_device_name, "Ebs": {"VolumeSize": EBS_SIZE_GB, "VolumeType": "gp3"}}
            ],
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "deployment_id", "Value": deployment_id},
                        {"Key": "Name", "Value": instance_name or f"maidenrun-{deployment_id}-feedback"},
                    ],
                }
            ],
        )
    except Exception:
        ec2.delete_security_group(GroupId=security_group_id)  # no instance, so nothing else would clean it up
        raise
    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("launched feedback stack instance %s for deployment %s", instance_id, deployment_id)

    public_ip = wait_for_public_ip(ec2, instance_id)
    return {
        "instance_id": instance_id,
        "public_ip": public_ip,
        "dashboard_token": dashboard_token,
        "security_group_id": security_group_id,
    }
