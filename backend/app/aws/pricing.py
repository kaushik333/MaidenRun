from datetime import UTC, datetime

# Illustrative on-demand rates (us-east-1) — the single source of truth for
# cost estimates shown in the control plane UI, replacing the client-side
# guess DeployForm.jsx's cost widget used before Phase 10.
EC2_HOURLY_RATES = {
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "m5.xlarge": 0.192,
    "g4dn.xlarge": 0.526,
    "g5.xlarge": 1.006,
}
EBS_GB_MONTH_RATE = 0.08  # gp3, $/GB-month
# Every instance here has exactly one public IPv4 address — its own, or an
# agent's fixed address — and AWS bills each one, attached or not.
PUBLIC_IPV4_HOURLY = 0.005
S3_GB_MONTH_RATE = 0.023  # S3 Standard, $/GB-month
HOURS_PER_MONTH = 730


def instance_hourly_rate(instance_type: str) -> float:
    """The instance itself plus its public IPv4 address (disk is separate)."""
    return EC2_HOURLY_RATES.get(instance_type, 0.0) + PUBLIC_IPV4_HOURLY


def estimate_ec2_cost(
    instance_type: str, ebs_size_gb: int, launched_at: datetime, now: datetime | None = None
) -> float:
    """One instance running from launched_at until now: compute, its public
    IPv4 address, and its EBS disk."""
    now = now or datetime.now(UTC)
    elapsed_hours = max(0.0, (now - launched_at).total_seconds() / 3600)
    return elapsed_hours * instance_hourly_rate(instance_type) + ebs_size_gb * EBS_GB_MONTH_RATE * (
        elapsed_hours / HOURS_PER_MONTH
    )


def estimate_s3_cost(
    size_bytes: int, uploaded_at: datetime, deleted_at: datetime | None, now: datetime | None = None
) -> float:
    """The bundle is deleted right after its health check (see s3_bundle.py's
    delete_bundle) — passing deleted_at freezes the estimate at whatever it
    was worth when billable storage actually stopped, instead of letting it
    grow forever for an object that's already gone."""
    now = now or datetime.now(UTC)
    elapsed_hours = max(0.0, ((deleted_at or now) - uploaded_at).total_seconds() / 3600)
    size_gb = size_bytes / (1024**3)
    return size_gb * S3_GB_MONTH_RATE * (elapsed_hours / HOURS_PER_MONTH)


def deployment_cost(record: dict, now: datetime | None = None) -> float | None:
    """Everything a deployment has cost since it was first deployed — never
    reset by a redeploy. Its main instance is counted continuously (a redeploy
    swaps instances but something is always running); each redeploy attempt's
    extra instance — the overlap while the new version builds, or a failed
    attempt until it's removed — is added on top from `extra_runs`. Stops at
    `terminated_at`, when billing stopped. None if the record lacks the sizes.
    """
    from app.aws.feedback_stack import EBS_SIZE_GB as FEEDBACK_EBS_SIZE_GB
    from app.aws.feedback_stack import INSTANCE_TYPE as FEEDBACK_INSTANCE_TYPE

    decisions = record.get("decisions") or {}
    if record["kind"] == "agent":
        instance_type, ebs_size_gb = decisions.get("instance_type"), decisions.get("ebs_size_gb")
        if not instance_type or ebs_size_gb is None:
            return None
    else:
        instance_type, ebs_size_gb = FEEDBACK_INSTANCE_TYPE, FEEDBACK_EBS_SIZE_GB

    terminated_at = record.get("terminated_at")
    cutoff = datetime.fromisoformat(terminated_at) if terminated_at else (now or datetime.now(UTC))
    total = estimate_ec2_cost(instance_type, ebs_size_gb, datetime.fromisoformat(record["deployed_at"]), now=cutoff)
    for run in record.get("extra_runs") or []:
        total += estimate_ec2_cost(
            instance_type,
            ebs_size_gb,
            datetime.fromisoformat(run["started_at"]),
            now=datetime.fromisoformat(run["ended_at"]),
        )

    # Feedback stacks also relay their code through S3 for a few minutes.
    if decisions.get("bundle_size_bytes") and decisions.get("bundle_uploaded_at"):
        deleted_at = decisions.get("bundle_deleted_at")
        total += estimate_s3_cost(
            decisions["bundle_size_bytes"],
            datetime.fromisoformat(decisions["bundle_uploaded_at"]),
            datetime.fromisoformat(deleted_at) if deleted_at else None,
        )
    return total
