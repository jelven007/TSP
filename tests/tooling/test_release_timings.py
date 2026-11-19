"""Summarize recorded intervals without double-counting retries or parallel hosts."""
import copy
import importlib
import importlib.util

import pytest


def summarize(receipts):
    assert importlib.util.find_spec("release.summarize_timings") is not None
    return importlib.import_module("release.summarize_timings").summarize(receipts)


def timestamp(second):
    return f"2026-11-19T00:00:{second:02d}+00:00"


def receipt(environment="local", start=0, end=30, step_start=5, step_end=15):
    return {
        "format": 1, "version": "0.4.67", "revision": "a" * 40,
        "plan_sha256": "b" * 64, "environment": environment, "status": "passed",
        "started_at": timestamp(start), "ended_at": timestamp(end),
        "steps": {"prepare": {
            "phase": "preflight", "status": "passed",
            "started_at": timestamp(step_start), "ended_at": timestamp(step_end),
            "elapsed_seconds": step_end - step_start, "prior_attempts": [],
        }},
    }


def test_parallel_receipts_use_interval_union_and_preserve_pipeline_span():
    result = summarize([receipt(), receipt("remote", 3, 40, 10, 20)])
    assert result["pipeline_span_seconds"] == 40
    assert result["recorded_step_active_seconds"] == 15
    assert result["unattributed_seconds"] == 25
    assert result["phases"]["preflight"]["active_seconds"] == 15
    assert result["phases"]["preflight"]["summed_elapsed_seconds"] == 20
    assert result["deployment_verified"] is False


def test_failed_attempts_are_counted_once_without_being_replaced_by_retry():
    record = receipt(step_start=20, step_end=25)
    row = record["steps"]["prepare"]
    row["prior_attempts"] = [{
        "phase": "preflight", "status": "failed",
        "started_at": timestamp(5), "ended_at": timestamp(10), "elapsed_seconds": 5,
    }]
    result = summarize([record])
    assert result["recorded_step_active_seconds"] == 10
    assert result["phases"]["preflight"]["attempts"] == 2
    assert result["phases"]["preflight"]["failed_attempts"] == 1


def test_reconciliation_interval_is_separate_from_original_action():
    record = receipt()
    record["steps"]["prepare"].update(
        reconciled=True, reconciled_at=timestamp(25), reconcile_elapsed_seconds=3,
    )
    result = summarize([record])
    assert result["recorded_step_active_seconds"] == 13
    assert result["reconciliation_seconds"] == 3


@pytest.mark.parametrize("invalid", ["duplicate", "reversed", "naive", "negative", "mixed_revision"])
def test_invalid_or_incompatible_receipts_are_rejected(invalid):
    record = receipt()
    records = [record]
    if invalid == "duplicate":
        records.append(copy.deepcopy(record))
    elif invalid == "reversed":
        record["ended_at"] = timestamp(0)
    elif invalid == "naive":
        record["started_at"] = "2026-11-19T00:00:00"
    elif invalid == "negative":
        record["steps"]["prepare"]["elapsed_seconds"] = -1
    else:
        records.append(receipt("remote"))
        records[1]["revision"] = "c" * 40
    with pytest.raises(ValueError):
        summarize(records)


def test_summary_does_not_copy_private_output_or_arguments():
    record = receipt()
    record["steps"]["prepare"].update(argv=["private-token"], stdout="private-token")
    assert "private-token" not in str(summarize([record]))


def test_evidence_intervals_include_failed_probes_without_double_counting_postchecks():
    record = receipt()
    record["evidence_checks"] = [
        {"kind": "preparation", "status": "passed", "started_at": timestamp(0),
         "ended_at": timestamp(3), "elapsed_seconds": 3},
        {"kind": "postcheck", "status": "passed", "started_at": timestamp(12),
         "ended_at": timestamp(15), "elapsed_seconds": 3},
        {"kind": "probe", "status": "failed", "started_at": timestamp(20),
         "ended_at": timestamp(22), "elapsed_seconds": 2},
    ]
    result = summarize([record])
    assert result["recorded_step_active_seconds"] == 15
    assert result["unattributed_seconds"] == 15
    assert result["evidence_seconds"] == 8
