"""Progress timing must not invalidate identical successful daily result payloads."""
from copy import deepcopy

import pytest

from app.services.strategy_results import ResultIdentity, StrategyResults
from app.strategy.execution_status import merge_result


def test_repeated_stage_receipt_keeps_payload_but_new_counts_update_it(tmp_path):
    store = StrategyResults(tmp_path)
    identity = ResultIdentity("sample", "示例", "v1", "{}", "{}", "null", "stock", "1m")
    progress = {
        "schema": "late_session_progress_v1", "as_of": "2026-10-22",
        "observed_at": "2026-10-22T14:30:30",
        "steps": {"ma1": {"state": "success", "processed": 5, "total": 5, "passed": 3,
                          "started_at": "2026-10-22T14:30:30", "finished_at": "2026-10-22T14:30:31",
                          "elapsed_ms": 1000, "scheduled_at": "2026-10-22T14:30:30"}},
    }
    result = {"as_of": "2026-10-22", "rows": [], "total": 0,
              "entry_data_quality": {"signal": {"progress": progress}}}
    first = store.save(identity, result, started_at=1)
    later = deepcopy(result)
    later_progress = later["entry_data_quality"]["signal"]["progress"]
    later_progress["observed_at"] = "2026-10-22T14:31:30"
    later_progress["steps"]["ma1"].update(
        started_at="2026-10-22T14:31:30", finished_at="2026-10-22T14:31:31", elapsed_ms=900)
    repeated = store.save(identity, later, started_at=2)
    assert repeated["_cache_token"] == first["_cache_token"]
    assert repeated["result"] == first["result"]
    assert repeated["summary"]["started_at"] == 2
    later_progress["steps"]["ma1"]["passed"] = 2
    changed = store.save(identity, later, started_at=3)
    assert changed["_cache_token"] != first["_cache_token"]
    assert changed["result"]["entry_data_quality"]["signal"]["progress"]["steps"]["ma1"]["passed"] == 2
    later_progress["steps"]["ma1"]["scheduled_at"] = "2026-10-22T14:32:00"
    rescheduled = store.save(identity, later, started_at=4)
    assert rescheduled["_cache_token"] != changed["_cache_token"]


def sample(at="14:30:43", passed=541):
    return merge_result(None, {
        "as_of": "2026-11-13", "rows": [], "total": 0, "computed_at": at,
        "entry_data_quality": {"signal": {"progress": {
            "schema": "late_session_progress_v1", "as_of": "2026-11-13",
            "bar_at": "2026-11-13T14:30:00", "cohort": "frozen_selection",
            "params": {"pool_up_min_pct": 60},
            "observed_at": "2026-11-13T" + at,
            "steps": {"breadth": {
                "state": "rejected", "processed": 3049, "total": 3049,
                "usable": 3048, "passed": passed, "missing": 1,
                "up_pct": passed / 3048 * 100,
                "started_at": "2026-11-13T" + at,
                "finished_at": "2026-11-13T" + at, "elapsed_ms": 75,
                "scheduled_at": "2026-11-13T14:30:15",
            }},
        }}},
    })


def step(saved):
    return saved["result"]["entry_data_quality"]["signal"]["progress"]["steps"]["breadth"]


@pytest.fixture
def archive(tmp_path):
    return StrategyResults(tmp_path), ResultIdentity(
        "sample", "示例", "v1", "{}", "{}", "null", "stock", "1m", daily_record=True)


def test_execution_clocks_do_not_overwrite_identical_receipt(archive):
    store, identity = archive
    first = store.save(identity, sample(), started_at=1)
    later = sample("14:54:59")
    later["execution_status"]["updated_at"] += 1000
    second = store.save(identity, later, started_at=2)
    assert second["_cache_token"] == first["_cache_token"]
    assert second["result"] == first["result"]


def test_metric_change_keeps_complete_first_receipt(archive):
    store, identity = archive
    first = store.save(identity, sample(), started_at=1)
    second = store.save(identity, sample("14:54:59", passed=600), started_at=2)
    assert step(second)["started_at"].endswith("14:54:59")
    assert step(second)["passed"] == 600
    assert step(second)["first_receipt"]["started_at"].endswith("14:30:43")
    assert step(second)["first_receipt"]["passed"] == 541
    assert step(second)["first_receipt"] == step(first)["first_receipt"]
    assert "first_receipt" not in step(second)["first_receipt"]
    assert step(store.read(identity, "2026-11-13")) == step(second)


@pytest.mark.parametrize("state", ["waiting", "failed", "skipped"])
def test_no_actual_calculation_does_not_claim_first(archive, state):
    store, identity = archive
    waiting = sample()
    waiting["entry_data_quality"]["signal"]["progress"]["steps"]["breadth"].update(
        state=state, processed=None, started_at=None, finished_at=None)
    saved = store.save(identity, waiting, started_at=1)
    assert step(saved).get("first_receipt") is None
    actual = store.save(identity, sample(), started_at=2)
    assert step(actual)["first_receipt"]["started_at"].endswith("14:30:43")


def test_pre_fix_historical_receipt_does_not_invent_first(archive):
    store, identity = archive
    # Insert an archive from the old implementation, without first_receipt.
    saved = store.save(identity, sample("14:54:59"), started_at=1)
    step(saved).pop("first_receipt", None)
    import json
    with store._local(write=True) as connection:
        connection.execute("UPDATE strategy_results SET value=? WHERE id=?",
                           (json.dumps(saved), saved["record_id"]))
    latest = store.save(identity, sample("14:55:00", passed=600), started_at=2)
    assert "first_receipt" in step(latest)
    assert step(latest)["first_receipt"] is None
    third = store.save(identity, sample("14:55:01", passed=601), started_at=3)
    assert step(third)["first_receipt"] is None


@pytest.mark.parametrize("change", ["scheduled_at", "bar_at", "cohort", "params", "definition", "day"])
def test_first_receipt_resets_for_new_execution_scope(archive, change):
    from dataclasses import replace
    store, identity = archive
    store.save(identity, sample(), started_at=1)
    later = sample("14:40:00", passed=600)
    progress = later["entry_data_quality"]["signal"]["progress"]
    if change == "scheduled_at":
        progress["steps"]["breadth"]["scheduled_at"] = "2026-11-13T14:39:15"
    elif change == "params":
        progress["params"] = {"pool_up_min_pct": 50}
    elif change == "definition":
        identity = replace(identity, strategy_version="v2")
    elif change == "day":
        later["as_of"] = progress["as_of"] = "2026-11-16"
    else:
        progress[change] = "changed"
    saved = store.save(identity, later, started_at=2)
    assert step(saved)["first_receipt"]["started_at"].endswith("14:40:00")
    assert step(saved)["first_receipt"]["passed"] == 600


def test_failed_and_out_of_order_run_cannot_replace_first(archive):
    store, identity = archive
    first = store.save(identity, sample(), started_at=2)
    old = store.save(identity, sample("14:29:00", passed=600), started_at=1)
    assert old["result"] == first["result"]
    failed = sample("14:54:59")
    failed["execution_status"].update(state="failed", reason="行情不可读")
    failed["entry_data_quality"]["signal"]["progress"]["steps"]["breadth"].update(
        state="failed", processed=None, started_at=None, finished_at=None)
    shown = store.save(identity, failed, started_at=3)
    assert step(shown)["first_receipt"] == step(first)["first_receipt"]
    assert store.read(identity, "2026-11-13")["result"] == first["result"]


def test_other_strategies_keep_existing_status_clock_semantics(archive):
    store, identity = archive
    result = sample()
    result.pop("entry_data_quality")
    first = store.save(identity, result, started_at=1)
    result["execution_status"]["updated_at"] += 1000
    second = store.save(identity, result, started_at=2)
    assert second["_cache_token"] != first["_cache_token"]
