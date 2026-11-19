"""Pair actual process samples, rejecting resets and missing observations."""
import copy
import importlib
import importlib.util

import pytest


def collector():
    assert importlib.util.find_spec("performance.collect_observability") is not None
    return importlib.import_module("performance.collect_observability")


def metric(instance, uptime, calls):
    return {"process": {"instance_id": instance, "uptime_seconds": uptime},
            "stages": {"compute.cleanup": {"calls": calls, "failures": 0, "duration_ms": {
                "sum": calls * 5, "histogram": {"bounds_ms": [10, 100], "counts": [calls, 0, 0]},
            }}}}


def sample(second, instance="worker", calls=1):
    return {"at": f"2026-11-19T01:30:{second:02d}+00:00",
            "targets": {"realtime": {"status": "passed", "metrics": metric(instance, second, calls)}}}


def test_window_pairs_stage_histograms_from_the_same_process():
    result = collector().window(sample(1), sample(11, calls=4), max_gap=15)
    assert result["targets"]["realtime"]["status"] == "passed"
    stage = result["targets"]["realtime"]["stages"]["compute.cleanup"]
    assert stage["requests"] == 3
    assert stage["p95_bucket_ms"] == {"lower": 0, "upper": 10}


@pytest.mark.parametrize("problem", ["restart", "gap", "missing", "counter_reset"])
def test_discontinuity_is_explicit_and_never_merged(problem):
    before, after = sample(1), sample(11, calls=4)
    if problem == "restart":
        after = sample(11, "new-worker", 4)
    elif problem == "gap":
        after = sample(31, calls=4)
    elif problem == "missing":
        after["targets"]["realtime"] = {"status": "failed"}
    else:
        before = sample(1, calls=9)
    result = collector().window(before, after, max_gap=15)
    assert result["targets"]["realtime"]["status"] != "passed"
    assert "stages" not in result["targets"]["realtime"]


def test_worker_source_timestamp_must_be_fresh_and_kind_must_match():
    value = {"kind": "realtime", "healthy": True, "captured_at": sample(1)["at"],
             "observability": metric("worker", 1, 1)}
    module = collector()
    assert module.normalize(value, name="realtime", now=sample(11)["at"], max_age=15)
    for changed in ("stale", "wrong_kind", "unhealthy"):
        bad = copy.deepcopy(value)
        if changed == "wrong_kind":
            bad["kind"] = "research"
        elif changed == "unhealthy":
            bad["healthy"] = False
        with pytest.raises(ValueError):
            module.normalize(bad, name="realtime",
                             now=sample(31 if changed == "stale" else 11)["at"], max_age=15)


def test_multiworker_samples_pair_by_identity_instead_of_http_routing_order(monkeypatch):
    module = collector()
    rows = [metric("api-one", 10, 1), metric("api-two", 10, 2)]
    monkeypatch.setattr(module, "read_adapter", lambda argv: {"process_samples": rows})
    config = {"targets": [{"name": "api", "argv": ["adapter"], "expected_processes": 2}],
              "max_gap_seconds": 60}
    before, _ = module.collect(config)
    rows[:] = [metric("api-two", 20, 5), metric("api-one", 20, 3)]
    after, _ = module.collect(config)
    before["at"], after["at"] = sample(1)["at"], sample(11)["at"]
    result = module.window(before, after, max_gap=60)
    assert set(result["targets"]) == {"api/api-one", "api/api-two"}
    assert result["targets"]["api/api-one"]["stages"]["compute.cleanup"]["requests"] == 2
    assert result["targets"]["api/api-two"]["stages"]["compute.cleanup"]["requests"] == 3


def test_missing_multiworker_process_does_not_certify_complete_role(monkeypatch):
    module = collector()
    monkeypatch.setattr(module, "read_adapter", lambda argv: {
        "process_samples": [metric("api-one", 10, 1)],
    })
    value, _ = module.collect({
        "targets": [{"name": "api", "argv": ["adapter"], "expected_processes": 2}],
        "max_gap_seconds": 60,
    })
    assert value["targets"]["api"]["status"] == "failed"
