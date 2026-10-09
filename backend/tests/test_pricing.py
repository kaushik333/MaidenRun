from datetime import UTC, datetime, timedelta

from app.aws.pricing import EBS_GB_MONTH_RATE, HOURS_PER_MONTH, S3_GB_MONTH_RATE, estimate_ec2_cost, estimate_s3_cost

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_estimate_ec2_cost_matches_hand_calculated_value() -> None:
    launched_at = NOW - timedelta(hours=10)
    cost = estimate_ec2_cost("t3.medium", ebs_size_gb=50, launched_at=launched_at, now=NOW)
    expected = 10 * 0.0416 + 50 * EBS_GB_MONTH_RATE * (10 / HOURS_PER_MONTH)
    assert cost == expected


def test_estimate_ec2_cost_unknown_instance_type_charges_zero_hourly_rate() -> None:
    launched_at = NOW - timedelta(hours=5)
    cost = estimate_ec2_cost("some.unlisted.type", ebs_size_gb=20, launched_at=launched_at, now=NOW)
    expected = 20 * EBS_GB_MONTH_RATE * (5 / HOURS_PER_MONTH)
    assert cost == expected


def test_estimate_s3_cost_grows_while_still_uploaded() -> None:
    uploaded_at = NOW - timedelta(hours=2)
    cost = estimate_s3_cost(size_bytes=1024**3, uploaded_at=uploaded_at, deleted_at=None, now=NOW)
    expected = 1 * S3_GB_MONTH_RATE * (2 / HOURS_PER_MONTH)
    assert cost == expected


def test_estimate_s3_cost_freezes_after_deletion() -> None:
    uploaded_at = NOW - timedelta(hours=2)
    deleted_at = NOW - timedelta(hours=1, minutes=55)  # deleted 5 minutes after upload

    cost_at_deletion = estimate_s3_cost(
        size_bytes=1024**3, uploaded_at=uploaded_at, deleted_at=deleted_at, now=deleted_at
    )
    cost_much_later = estimate_s3_cost(
        size_bytes=1024**3, uploaded_at=uploaded_at, deleted_at=deleted_at, now=NOW + timedelta(days=30)
    )
    assert cost_much_later == cost_at_deletion
