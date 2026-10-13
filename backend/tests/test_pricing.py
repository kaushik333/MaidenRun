from datetime import UTC, datetime, timedelta

from app.aws.pricing import (
    EBS_GB_MONTH_RATE,
    HOURS_PER_MONTH,
    PUBLIC_IPV4_HOURLY,
    S3_GB_MONTH_RATE,
    deployment_cost,
    estimate_ec2_cost,
    estimate_s3_cost,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_estimate_ec2_cost_matches_hand_calculated_value() -> None:
    launched_at = NOW - timedelta(hours=10)
    cost = estimate_ec2_cost("t3.medium", ebs_size_gb=50, launched_at=launched_at, now=NOW)
    expected = 10 * (0.0416 + PUBLIC_IPV4_HOURLY) + 50 * EBS_GB_MONTH_RATE * (10 / HOURS_PER_MONTH)
    assert cost == expected


def test_estimate_ec2_cost_unknown_instance_type_still_charges_its_public_ip_and_disk() -> None:
    launched_at = NOW - timedelta(hours=5)
    cost = estimate_ec2_cost("some.unlisted.type", ebs_size_gb=20, launched_at=launched_at, now=NOW)
    expected = 5 * PUBLIC_IPV4_HOURLY + 20 * EBS_GB_MONTH_RATE * (5 / HOURS_PER_MONTH)
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


def _agent(**extra) -> dict:
    return {
        "kind": "agent", "deployed_at": (NOW - timedelta(hours=10)).isoformat(),
        "decisions": {"instance_type": "t3.small", "ebs_size_gb": 20}, **extra,
    }


def test_deployment_cost_runs_continuously_from_the_first_deploy() -> None:
    assert deployment_cost(_agent(), now=NOW) == estimate_ec2_cost(
        "t3.small", 20, NOW - timedelta(hours=10), now=NOW
    )


def test_deployment_cost_adds_every_redeploy_overlap_and_failed_attempt() -> None:
    runs = [
        {"started_at": (NOW - timedelta(hours=6)).isoformat(), "ended_at": (NOW - timedelta(hours=5.9)).isoformat(),
         "outcome": "redeployed"},
        {"started_at": (NOW - timedelta(hours=2)).isoformat(), "ended_at": (NOW - timedelta(hours=1.75)).isoformat(),
         "outcome": "failed"},
    ]
    one_hour_of_a_second_instance = estimate_ec2_cost("t3.small", 20, NOW - timedelta(hours=1), now=NOW)
    expected = deployment_cost(_agent(), now=NOW) + 0.35 * one_hour_of_a_second_instance
    assert abs(deployment_cost(_agent(extra_runs=runs), now=NOW) - expected) < 1e-12


def test_deployment_cost_stops_when_terminated() -> None:
    terminated = _agent(terminated_at=(NOW - timedelta(hours=4)).isoformat())
    assert deployment_cost(terminated, now=NOW) == estimate_ec2_cost(
        "t3.small", 20, NOW - timedelta(hours=10), now=NOW - timedelta(hours=4)
    )
