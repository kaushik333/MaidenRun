from datetime import datetime, timezone

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
S3_GB_MONTH_RATE = 0.023  # S3 Standard, $/GB-month
HOURS_PER_MONTH = 730


def estimate_ec2_cost(
    instance_type: str, ebs_size_gb: int, launched_at: datetime, now: datetime | None = None
) -> float:
    now = now or datetime.now(timezone.utc)
    elapsed_hours = max(0.0, (now - launched_at).total_seconds() / 3600)
    hourly_rate = EC2_HOURLY_RATES.get(instance_type, 0.0)
    return elapsed_hours * hourly_rate + ebs_size_gb * EBS_GB_MONTH_RATE * (elapsed_hours / HOURS_PER_MONTH)


def estimate_s3_cost(
    size_bytes: int, uploaded_at: datetime, deleted_at: datetime | None, now: datetime | None = None
) -> float:
    """The bundle is deleted right after its health check (see s3_bundle.py's
    delete_bundle) — passing deleted_at freezes the estimate at whatever it
    was worth when billable storage actually stopped, instead of letting it
    grow forever for an object that's already gone."""
    now = now or datetime.now(timezone.utc)
    elapsed_hours = max(0.0, ((deleted_at or now) - uploaded_at).total_seconds() / 3600)
    size_gb = size_bytes / (1024**3)
    return size_gb * S3_GB_MONTH_RATE * (elapsed_hours / HOURS_PER_MONTH)
