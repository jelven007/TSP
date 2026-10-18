"""Ensure partial load, leaked data and interrupted clocks never certify a soak."""
from copy import deepcopy

import pytest

from scripts.run_active_acceptance import evaluate


@pytest.fixture
def complete_run():
    return {
        "duration_hours": 72, "round_seconds": 20, "observed_seconds": 259200,
        "isolation_violations": 0, "private_probe_timeouts": 0,
        "research": {"succeeded": 200, "failed": 0, "user_indices": list(range(100))},
        "requests": {"market.daily": {"count": 100000, "errors": 0,
                                     "histogram": [0, 100000, 0, 0, 0, 0, 0, 0, 0, 0]}},
        "users": [{
            "user_id": f"user{i}", "tenant_id": f"tenant{i}", "rounds": 12960,
            "baseline_rounds": 0, "max_activity_gap_s": 25, "stream_samples": 17000,
            "stream_up_samples": 16900, "public_events": 200, "private_received": 4320,
            "stream_up_seconds": 259000, "stream_down_seconds": 200,
        } for i in range(100)],
    }


def test_full_evidence_certifies(complete_run):
    assert evaluate(complete_run)["certified_72h_100_active_users"]


@pytest.mark.parametrize("fault", [
    "99_users", "same_tenant", "idle_user", "sse_down", "no_market", "leak", "restart",
    "clock_short", "observer_gap", "compute_failed", "compute_pending", "missing_probe",
    "probe_in_flight", "slow_requests",
])
def test_partial_or_failed_evidence_cannot_pass(complete_run, fault):
    record = deepcopy(complete_run)
    if fault == "99_users":
        record["users"].pop()
    elif fault == "same_tenant":
        record["users"][1]["tenant_id"] = record["users"][0]["tenant_id"]
    elif fault == "idle_user":
        record["users"][1]["rounds"] = 10
    elif fault == "sse_down":
        record["users"][1]["stream_up_samples"] = 100
        record["users"][1]["stream_up_seconds"] = 100
        record["users"][1]["stream_down_seconds"] = 259100
    elif fault == "no_market":
        record["users"][1]["public_events"] = 0
    elif fault == "leak":
        record["isolation_violations"] = 1
    elif fault == "restart":
        record["restarts"] = 1
    elif fault == "clock_short":
        record["observed_seconds"] = 259000
    elif fault == "observer_gap":
        record["max_sample_gap_s"] = 80
    elif fault == "compute_failed":
        record["research"]["failed"] = 1
    elif fault == "compute_pending":
        record["research"]["in_flight"] = 12
    elif fault == "missing_probe":
        record["private_probe_timeouts"] = 1
    elif fault == "probe_in_flight":
        record["pending_private_probes"] = 10
    elif fault == "slow_requests":
        record["requests"]["market.daily"]["histogram"] = [0] * 6 + [100000] + [0] * 3
    result = evaluate(record)
    assert result["status"] == "failed"
    assert not result["certified_72h_100_active_users"]


def test_successful_smoke_does_not_claim_72h(complete_run):
    complete_run["duration_hours"] = .1
    assert evaluate(complete_run)["status"] == "succeeded"
    assert not evaluate(complete_run)["certified_72h_100_active_users"]


def test_short_ticket_renewal_measures_elapsed_time_not_aliased_samples(complete_run):
    complete_run["duration_hours"] = 5 / 60
    complete_run["observed_seconds"] = 300
    for user in complete_run["users"]:
        user.update(stream_up_seconds=299.5, stream_down_seconds=.5,
                    stream_samples=21, stream_up_samples=20)
    assert evaluate(complete_run)["checks"]["sse_available_99_percent_per_user"]
