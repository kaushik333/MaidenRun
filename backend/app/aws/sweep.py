import boto3
from botocore.exceptions import ClientError

from app.aws.ec2 import PARAMETER_NAMESPACE, delete_secret_parameters, release_address, terminate_instance
from app.aws.pricing import PUBLIC_IPV4_HOURLY, instance_hourly_rate
from app.aws.s3_bundle import BUCKET_NAME_PREFIX, cleanup_bundle_bucket
from app.core.logging import logger

# AWS bills a fixed address whether or not anything uses it.
ADDRESS_HOURLY_USD = PUBLIC_IPV4_HOURLY

# Every resource this platform creates carries one of these: instances, fixed
# addresses and security groups a deployment_id tag / maidenrun-* name, secrets the
# /maidenrun/<deployment_id>/ path. That's what lets a sweep find anything the
# control plane lost track of (a crash mid-deploy, a deleted data/ folder, a
# region nobody is looking at).
# "shutting-down" is fetched only so its security group counts as in use until
# AWS finishes terminating it (about a minute); the instance itself isn't listed.
ACTIVE_STATES = ["pending", "running", "shutting-down", "stopping", "stopped"]


def scan_region(session: boto3.Session, running_ids: set[str], warnings: set[str]) -> list[dict]:
    """Platform resources in this region that belong to no running deployment.
    A missing permission for the secrets check is reported in `warnings`
    rather than failing the scan — instances still billing matter most."""
    region = session.region_name
    ec2 = session.client("ec2")
    items: list[dict] = []

    instances = [
        instance
        for page in ec2.get_paginator("describe_instances").paginate(
            Filters=[
                {"Name": "tag-key", "Values": ["deployment_id"]},
                {"Name": "instance-state-name", "Values": ACTIVE_STATES},
            ]
        )
        for reservation in page["Reservations"]
        for instance in reservation["Instances"]
    ]
    for instance in instances:
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        if tags.get("deployment_id") in running_ids or instance["State"]["Name"] == "shutting-down":
            continue
        # A stray feedback stack can't be exported first — its dashboard token
        # lived in the deployment record that's gone — so say what's at stake.
        is_feedback = any(g["GroupName"].startswith("maidenrun-feedback") for g in instance["SecurityGroups"])
        detail = f"{instance['InstanceType']}, {instance['State']['Name']}"
        if is_feedback:
            detail += " — a feedback stack: deleting it destroys any feedback on it, with no export"
        items.append({
            "kind": "instance",
            "region": region,
            "id": instance["InstanceId"],
            "deployment_id": tags.get("deployment_id"),
            "label": tags.get("Name", instance["InstanceId"]),
            "detail": detail,
            "hourly_usd": instance_hourly_rate(instance["InstanceType"]),
            "destroys_feedback": is_feedback,
        })

    addresses = ec2.describe_addresses(Filters=[{"Name": "tag-key", "Values": ["deployment_id"]}])["Addresses"]
    for address in addresses:
        tags = {tag["Key"]: tag["Value"] for tag in address.get("Tags", [])}
        attached = bool(address.get("AssociationId"))
        if attached and tags.get("deployment_id") in running_ids:
            continue
        items.append({
            "kind": "address",
            "region": region,
            "id": address["AllocationId"],
            "deployment_id": tags.get("deployment_id"),
            "label": f"{address['PublicIp']} (fixed address)",
            "detail": "attached to a leftover instance" if attached else "not attached to anything",
            "hourly_usd": ADDRESS_HOURLY_USD,
        })

    # A group is in use only while some live platform instance references it.
    used_groups = {group["GroupId"] for instance in instances for group in instance["SecurityGroups"]}
    groups = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": ["maidenrun-*"]}])
    for group in groups["SecurityGroups"]:
        if group["GroupId"] not in used_groups:
            items.append({
                "kind": "security_group",
                "region": region,
                "id": group["GroupId"],
                "label": group["GroupName"],
                "detail": "security group, not attached to anything",
            })

    pages = session.client("ssm").get_paginator("describe_parameters").paginate(
        ParameterFilters=[{"Key": "Path", "Option": "Recursive", "Values": [PARAMETER_NAMESPACE]}]
    )
    secrets_by_deployment: dict[str, int] = {}
    try:
        for page in pages:
            for param in page["Parameters"]:
                deployment_id = param["Name"].split("/")[2]  # /maidenrun/<deployment_id>/<NAME>
                secrets_by_deployment[deployment_id] = secrets_by_deployment.get(deployment_id, 0) + 1
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("AccessDenied", "AccessDeniedException"):
            raise
        warnings.add(
            "Secrets weren't checked: your IAM policy is missing ssm:DescribeParameters — update it from "
            "infra/iam-policy.json (shown in the app's AWS one-time setup)."
        )
    for deployment_id, count in secrets_by_deployment.items():
        if deployment_id not in running_ids:
            items.append({
                "kind": "secrets",
                "region": region,
                "id": deployment_id,
                "label": f"Secrets of deployment {deployment_id}",
                "detail": f"{count} SSM parameter(s) — API keys/tokens, still readable",
            })
    return items


def scan_bucket(session: boto3.Session) -> list[dict]:
    """The account's feedback-stack relay bucket, if it exists. It's empty
    and free between deploys, and recreated automatically on the next one."""
    bucket = f"{BUCKET_NAME_PREFIX}-{session.client('sts').get_caller_identity()['Account']}"
    s3 = session.client("s3")
    try:
        response = s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("404", "NoSuchBucket"):
            return []
        raise
    region = response.get("BucketRegion") or session.region_name
    count = session.client("s3", region_name=region).list_objects_v2(Bucket=bucket).get("KeyCount", 0)
    return [{
        "kind": "bucket",
        "region": region,
        "id": bucket,
        "label": bucket,
        "detail": f"S3 relay bucket, {count} object(s) — recreated automatically on the next feedback deploy",
    }]


def delete_leftover(session: boto3.Session, kind: str, resource_id: str) -> None:
    """Deletes one item returned by a scan. Raises ClientError on failure."""
    if kind == "instance":
        terminate_instance(session, resource_id)
    elif kind == "address":
        release_address(session, resource_id)
    elif kind == "security_group":
        session.client("ec2").delete_security_group(GroupId=resource_id)
    elif kind == "secrets":
        delete_secret_parameters(session, resource_id)
    elif kind == "bucket":
        cleanup_bundle_bucket(session)
    logger.info("sweep: deleted %s %s in %s", kind, resource_id, session.region_name)
