import time
import urllib.error
import urllib.request

import boto3

from app.core.config import settings
from app.core.logging import logger

SECURITY_GROUP_NAME = "agent-feedback-deploy-app"
INSTANCE_ROLE_NAME = "agent-feedback-deploy-instance-role"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
PARAMETER_NAMESPACE = "/agent-feedback-deploy"

# Deterministic provider -> env-var-name mapping (see IDEA.md's "API key ->
# environment variable mapping is a fixed convention" security bullet).
PROVIDER_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "azure_openai": "AZURE_OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

DOCKER_INSTALL = (
    "dnf install -y docker git\n"
    "systemctl enable --now docker\n"
    "mkdir -p /usr/libexec/docker/cli-plugins\n"
    "curl -sSL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 "
    "-o /usr/libexec/docker/cli-plugins/docker-compose\n"
    "chmod +x /usr/libexec/docker/cli-plugins/docker-compose"
)


class Ec2LaunchError(Exception):
    """Raised when an instance doesn't come up as expected (bad account
    setup, AWS-side provisioning failure — a 502-shaped problem)."""


class HealthCheckTimeoutError(Ec2LaunchError):
    """Raised specifically when a port never answers within its timeout —
    the instance itself provisioned fine, this is a narrower, 504-shaped
    problem. A subclass of Ec2LaunchError so callers that only care about
    "something in provisioning went wrong" can still catch the base class."""


def write_secret_parameters(session: boto3.Session, deployment_id: str, secrets: dict[str, str]) -> None:
    """Writes each secret as a SecureString under /agent-feedback-deploy/<deployment_id>/<name>.

    Never logged, never written anywhere else — the only other place these
    values are read back is the instance's own boot script, over the AWS
    API, using its own narrowly-scoped instance role.
    """
    ssm = session.client("ssm")
    for name, value in secrets.items():
        ssm.put_parameter(
            Name=f"{PARAMETER_NAMESPACE}/{deployment_id}/{name}",
            Value=value,
            Type="SecureString",
            Tags=[{"Key": "deployment_id", "Value": deployment_id}],
        )
    logger.info("wrote %d secret parameter(s) for deployment %s", len(secrets), deployment_id)


def get_default_vpc_id(ec2) -> str:
    """Shared by the app and feedback-stack security group setup."""
    default_vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not default_vpcs:
        raise Ec2LaunchError(
            "No default VPC found in this account/region. The platform relies on the "
            "account's default VPC and subnets for v0 — create one (or restore it via "
            "the VPC console's 'Actions > Create default VPC') and try again."
        )
    return default_vpcs[0]["VpcId"]


def ensure_app_security_group(session: boto3.Session) -> str:
    """Creates (or reuses) the security group that opens only 8001 for app instances."""
    ec2 = session.client("ec2")
    existing = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [SECURITY_GROUP_NAME]}])[
        "SecurityGroups"
    ]
    if existing:
        return existing[0]["GroupId"]

    group_id = ec2.create_security_group(
        GroupName=SECURITY_GROUP_NAME,
        Description="Agent Feedback Deploy: app instance, port 8001 only",
        VpcId=get_default_vpc_id(ec2),
    )["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": settings.app_port,
                "ToPort": settings.app_port,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )
    return group_id


def latest_al2023_ami(session: boto3.Session) -> tuple[str, str]:
    """Returns (ami_id, root_device_name) for the latest Amazon Linux 2023 AMI."""
    ami_id = session.client("ssm").get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
    image = session.client("ec2").describe_images(ImageIds=[ami_id])["Images"][0]
    return ami_id, image["RootDeviceName"]


def render_user_data(repo_url: str, entry_point: str, secret_names: list[str], deployment_id: str, region: str) -> str:
    """Builds the boot script: install Docker, fetch secrets from SSM, clone, run.

    Secret *values* never appear here — only the parameter names and the
    aws ssm command used to fetch them at boot. For the Dockerfile-only
    path, secrets are passed to `docker run` as bare `-e NAME` flags (no
    `=value`), which tells Docker to read the value from this script's own
    shell environment rather than embedding it in the command line, where
    it would otherwise be visible to anything that can list processes.
    """
    export_lines = "\n".join(
        f'export {name}="$(aws ssm get-parameter --name "{PARAMETER_NAMESPACE}/{deployment_id}/{name}" '
        f'--with-decryption --query Parameter.Value --output text --region {region})"'
        for name in secret_names
    )

    if entry_point == "compose":
        run_command = "docker compose up -d"
    else:
        env_flags = " ".join(f"-e {name}" for name in secret_names)
        run_command = (
            "docker build -t agent-app .\n"
            f"docker run -d --restart unless-stopped -p 8001:8001 {env_flags} agent-app"
        )

    return (
        "#!/bin/bash\n"
        "set -euo pipefail\n\n"
        f"{DOCKER_INSTALL}\n\n"
        f"{export_lines}\n\n"
        f'git clone --depth 1 "{repo_url}" /opt/agent-repo\n'
        "cd /opt/agent-repo\n\n"
        f"{run_command}\n"
    )


def wait_for_public_ip(ec2, instance_id: str, timeout_seconds: int = 30) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
        public_ip = instance.get("PublicIpAddress")
        if public_ip:
            return public_ip
        time.sleep(1)
    raise Ec2LaunchError(f"Instance {instance_id} did not get a public IP within {timeout_seconds}s")


def wait_for_port_health(public_ip: str, port: int, timeout_seconds: int, poll_interval_seconds: float = 2.0) -> None:
    """Blocks until something answers on <public_ip>:<port>, or raises HealthCheckTimeoutError on timeout.

    "Answers" means any HTTP response at all, even a 4xx/5xx — for the app
    instance the platform has no idea what a given user's agent UI actually
    returns, only that something is now listening, rather than the
    connection being refused (still booting) or timing out. Shared by the
    app instance (:8001) and the feedback stack (:8002/:8003).
    """
    url = f"http://{public_ip}:{port}/"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=5)
        except urllib.error.HTTPError:
            logger.info("healthy at %s", url)
            return
        except (urllib.error.URLError, OSError):
            time.sleep(poll_interval_seconds)
        else:
            logger.info("healthy at %s", url)
            return

    raise HealthCheckTimeoutError(
        f"Instance at {public_ip} did not respond on port {port} within "
        f"{timeout_seconds}s. It may still be booting — check /var/log/cloud-init-output.log "
        "on the instance, or increase the health-check timeout and try again."
    )


def launch_app_instance(
    session: boto3.Session,
    deployment_id: str,
    repo_url: str,
    entry_point: str,
    instance_type: str,
    ebs_size_gb: int,
    secrets: dict[str, str],
    instance_name: str | None = None,
) -> dict:
    """Provisions the app EC2 instance and returns {"instance_id", "public_ip"}.

    Note: this returns as soon as AWS assigns a public IP (seconds), not
    once the agent itself is actually serving :8001 — Docker install +
    image build/pull inside user-data takes longer than that and happens
    in the background after this returns.
    """
    write_secret_parameters(session, deployment_id, secrets)
    security_group_id = ensure_app_security_group(session)
    ami_id, root_device_name = latest_al2023_ami(session)
    user_data = render_user_data(
        repo_url=repo_url,
        entry_point=entry_point,
        secret_names=list(secrets.keys()),
        deployment_id=deployment_id,
        region=session.region_name,
    )

    ec2 = session.client("ec2")
    response = ec2.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        SecurityGroupIds=[security_group_id],
        IamInstanceProfile={"Name": INSTANCE_ROLE_NAME},
        UserData=user_data,
        BlockDeviceMappings=[{"DeviceName": root_device_name, "Ebs": {"VolumeSize": ebs_size_gb, "VolumeType": "gp3"}}],
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "deployment_id", "Value": deployment_id},
                    # Never leave an instance nameless in the console — falls back to
                    # something identifiable if the user didn't type one.
                    {"Key": "Name", "Value": instance_name or f"agent-feedback-deploy-{deployment_id}"},
                ],
            }
        ],
    )
    instance_id = response["Instances"][0]["InstanceId"]
    logger.info("launched app instance %s for deployment %s", instance_id, deployment_id)

    public_ip = wait_for_public_ip(ec2, instance_id)
    return {"instance_id": instance_id, "public_ip": public_ip}


def terminate_instance(session: boto3.Session, instance_id: str) -> None:
    """Terminates one EC2 instance. Idempotent from the caller's perspective —
    AWS accepts a terminate call on an already-terminated instance without error."""
    session.client("ec2").terminate_instances(InstanceIds=[instance_id])
    logger.info("terminated instance %s", instance_id)
