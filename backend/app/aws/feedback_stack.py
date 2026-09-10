import boto3

from app.aws.ec2 import get_default_vpc_id
from app.core.config import settings

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


# launch_feedback_stack() intentionally removed — feedback-stack provisioning
# is being rebuilt as its own standalone action (not bundled into /deploy),
# delivering feedback-stack/'s code plus the user's uploaded questionnaire via
# an ephemeral S3 object rather than the git-clone approach this used to have.
# See plan.md, Phase 7, for the design; not yet implemented.
