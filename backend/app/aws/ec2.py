import time
import urllib.error
import urllib.request

import boto3

from app.core.config import settings
from app.core.logging import logger

SECURITY_GROUP_NAME = "agentdock-app"
INSTANCE_ROLE_NAME = "agentdock-instance-role"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
PARAMETER_NAMESPACE = "/agentdock"
# Reserved name, kept out of the agent's own secrets dict so a private-repo
# token is never exported into the agent's process environment alongside
# its real env vars.
REPO_TOKEN_PARAM_NAME = "__repo_token"

DOCKER_INSTALL = (
    "dnf install -y docker git\n"
    "systemctl enable --now docker\n"
    "mkdir -p /usr/libexec/docker/cli-plugins\n"
    # Diagnostic breadcrumbs, cheap to leave in permanently — cost nothing
    # and already earned their keep once, ruling out disk space/permissions/
    # SELinux as candidates for a `curl: (23)` failure that turned out to be
    # the pipe-to-grep bug below instead.\n"
    'echo "DIAG df: $(df -h / | tail -1)"\n'
    'echo "DIAG selinux: $(getenforce 2>&1 || echo not-installed)"\n'
    'echo "DIAG dir: $(ls -ld /usr/libexec/docker/cli-plugins 2>&1)"\n'
    'echo "DIAG whoami: $(whoami)"\n'
    "curl -fsSL --retry 3 https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 "
    "-o /usr/libexec/docker/cli-plugins/docker-compose\n"
    "chmod +x /usr/libexec/docker/cli-plugins/docker-compose\n"
    # Amazon Linux 2023's `docker` package doesn't pull in a buildx plugin as
    # a dependency, and `docker compose` refuses to build any service with a
    # `build:` block without one (fails with "compose build requires buildx
    # 0.17.0 or later") — buildx's release assets are version-qualified
    # filenames (no stable "latest" alias like compose has), so the tag has
    # to be resolved via the GitHub API first. Captured into a variable
    # *before* parsing it, rather than piped straight into `grep -m1` — `-m1`
    # exits the instant it finds a match, and curl (still mid-transfer on a
    # multi-KB response) gets a broken pipe from that early exit, which
    # surfaces as an opaque `curl: (23) Failure writing output to
    # destination` with no indication it was ever about a pipe.\n"
    'BUILDX_RELEASE_JSON="$(curl -fsSL --retry 3 https://api.github.com/repos/docker/buildx/releases/latest)"\n'
    "BUILDX_VERSION=\"$(sed -E -n 's/.*\"tag_name\": *\"([^\"]+)\".*/\\1/p' <<< \"$BUILDX_RELEASE_JSON\" | head -1)\"\n"
    'curl -fsSL --retry 3 "https://github.com/docker/buildx/releases/download/${BUILDX_VERSION}/'
    'buildx-${BUILDX_VERSION}.linux-amd64" -o /usr/libexec/docker/cli-plugins/docker-buildx\n'
    "chmod +x /usr/libexec/docker/cli-plugins/docker-buildx"
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
    """Writes each secret as a SecureString under /agentdock/<deployment_id>/<name>.

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
        Description="AgentDock: app instance, port 8001 only",
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


def render_user_data(
    repo_url: str,
    entry_point: str,
    secret_names: list[str],
    deployment_id: str,
    region: str,
    needs_repo_token: bool = False,
) -> str:
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

    if entry_point == "compose" and secret_names:
        # Compose only uses this script's exported vars for ${VAR}
        # interpolation — it never passes them into containers on its own.
        # This override (on the instance, outside the user's clone) lists each
        # secret by bare name, no value, on every service, so Compose copies
        # the value in from this shell's environment at `up` time.
        env_lines = "".join(f'    echo "      - {name}"\n' for name in secret_names)
        run_command = (
            'COMPOSE_MAIN="$(ls docker-compose*.y*ml)"\n'
            "mkdir -p /opt/agentdock\n"
            "{\n"
            '  echo "services:"\n'
            '  for svc in $(docker compose -f "$COMPOSE_MAIN" config --services); do\n'
            '    echo "  \\"$svc\\":"\n'
            '    echo "    environment:"\n'
            f"{env_lines}"
            "  done\n"
            "} > /opt/agentdock/compose.env.yml\n"
            'docker compose -f "$COMPOSE_MAIN" -f /opt/agentdock/compose.env.yml up -d'
        )
    elif entry_point == "compose":
        run_command = "docker compose up -d"
    else:
        env_flags = " ".join(f"-e {name}" for name in secret_names)
        run_command = (
            "docker build -t agent-app .\n"
            f"docker run -d --restart unless-stopped -p 8001:8001 {env_flags} agent-app"
        )

    if needs_repo_token:
        # Same reasoning as every other secret: user-data is visible to
        # anyone who can call describe-instance-attribute on this instance,
        # so the token is fetched from SSM at boot and substituted into the
        # clone URL here in bash — it never appears in this script's own
        # (Python-side, static) text.
        token_param = f"{PARAMETER_NAMESPACE}/{deployment_id}/{REPO_TOKEN_PARAM_NAME}"
        scheme, sep, rest = repo_url.partition("://")
        clone_lines = (
            f'REPO_TOKEN="$(aws ssm get-parameter --name "{token_param}" --with-decryption '
            f'--query Parameter.Value --output text --region {region})"\n'
            f'git clone --depth 1 "{scheme}{sep}${{REPO_TOKEN}}@{rest}" /opt/agent-repo\n'
        )
    else:
        clone_lines = f'git clone --depth 1 "{repo_url}" /opt/agent-repo\n'

    return (
        "#!/bin/bash\n"
        "set -euo pipefail\n\n"
        f"{DOCKER_INSTALL}\n\n"
        f"{export_lines}\n\n"
        f"{clone_lines}"
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
    github_token: str | None = None,
) -> dict:
    """Provisions the app EC2 instance and returns {"instance_id", "public_ip"}.

    Note: this returns as soon as AWS assigns a public IP (seconds), not
    once the agent itself is actually serving :8001 — Docker install +
    image build/pull inside user-data takes longer than that and happens
    in the background after this returns.
    """
    write_secret_parameters(session, deployment_id, secrets)
    if github_token:
        # Written separately from `secrets` — it authenticates the clone,
        # it is not one of the agent's own env vars, and must never be
        # exported into the agent's process alongside them.
        write_secret_parameters(session, deployment_id, {REPO_TOKEN_PARAM_NAME: github_token})
    security_group_id = ensure_app_security_group(session)
    ami_id, root_device_name = latest_al2023_ami(session)
    user_data = render_user_data(
        repo_url=repo_url,
        entry_point=entry_point,
        secret_names=list(secrets.keys()),
        deployment_id=deployment_id,
        region=session.region_name,
        needs_repo_token=bool(github_token),
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
                    {"Key": "Name", "Value": instance_name or f"agentdock-{deployment_id}"},
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
