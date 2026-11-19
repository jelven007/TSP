"""Reject invalid windows instead of averaging cumulative process percentiles."""
import copy
import importlib
import importlib.util

import pytest


def sample(instance, uptime, counts, total):
    return {
        "process": {"instance_id": instance, "uptime_seconds": uptime},
        "totals": {"requests": sum(counts), "duration_ms": {
            "sum": total, "p95": 9999,
            "histogram": {"bounds_ms": [10, 100], "counts": counts},
        }},
    }


def summarize(pairs):
    assert importlib.util.find_spec("performance.summarize_observability") is not None
    return importlib.import_module("performance.summarize_observability").summarize(pairs)


def test_windows_merge_bucket_deltas_instead_of_percentiles():
    result = summarize([
        (sample("a", 1, [2, 0, 0], 10), sample("a", 2, [3, 1, 0], 80)),
        (sample("b", 10, [0, 1, 0], 50), sample("b", 11, [1, 2, 0], 160)),
    ])
    assert result["requests"] == 4
    assert result["histogram"]["counts"] == [2, 2, 0]
    assert result["average_ms"] == 45
    assert result["p95_bucket_ms"] == {"lower": 10, "upper": 100}


def test_overflow_reports_unbounded_interval():
    result = summarize([(sample("a", 1, [0, 0, 0], 0), sample("a", 2, [0, 0, 1], 1000))])
    assert result["p95_bucket_ms"] == {"lower": 100, "upper": None}


@pytest.mark.parametrize("change", [
    "reset", "clock", "bounds", "negative", "duplicate_process",
])
def test_invalid_windows_fail_closed(change):
    before = sample("a", 1, [2, 0, 0], 10)
    after = sample("a", 2, [3, 1, 0], 80)
    pairs = [(before, after)]
    if change == "reset":
        after["process"]["instance_id"] = "reset"
    elif change == "clock":
        after["process"]["uptime_seconds"] = 0
    elif change == "bounds":
        after["totals"]["duration_ms"]["histogram"]["bounds_ms"] = [20, 100]
    elif change == "negative":
        after = sample("a", 2, [1, 0, 0], 5)
        pairs = [(before, after)]
    else:
        pairs.append(copy.deepcopy(pairs[0]))
    with pytest.raises(ValueError):
        summarize(pairs)
