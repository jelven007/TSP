"""S02 input waits before Compute; ready stages consume only the sealed frame."""
from copy import deepcopy

import pytest
from test_strategy_input_manifest import build, minutes, request


def test_frozen_context_never_reads_local_market_or_adjustments(monkeypatch):
    monkeypatch.setattr("app.services.strategy_input_binding.algorithm_sha256", lambda: "b" * 64)
    from app.services.strategy_input_binding import frozen_context
    value = build(minutes())
    context = frozen_context(value)
    assert context.history.height == 80
    assert context.daily_pool_snapshots["custom_mvxxcp88"]["symbols"] == request()["pool"]["symbols"]
    assert context.selection_snapshots["custom_mvxxcp88"]["rows"] == value["input"]["selection"]["rows"]
    assert context.source_input_sha256 == value["input_sha256"]


def test_waiting_input_produces_a_receipt_without_a_compute_request(monkeypatch):
    from test_monitor_phase_processes import NOW, payload

    from app.services.strategy_input_binding import defer_input
    task = payload()
    sid = task["strategy_task_ids"][0]
    task["s02_input_status"] = {sid: {"stocks": {"state": "waiting_input", "reason": "输入未同步"}}}
    result = defer_input(task, sid, "stocks", NOW)
    saved = result["results"][sid]["monitor_tasks"]["buy"]["signals"]
    step = next(iter(saved.values()))["stocks"]
    assert not step["terminal"]
    assert step["state"] == "waiting_input"
    assert not step["errors"]
    assert result["events"] == []


def test_same_definition_can_bind_authority_pool_without_overwriting_saved_local_scope():
    from app.services.strategy_input_binding import accept
    req = request()
    task = {"daily_pool_snapshots": {req["strategy_id"]: {"symbols": ["600999.SH"]}},
            "selection_snapshots": {}, "strategy_input_manifests": {}}
    original = deepcopy(task)
    accept(task, req, "stocks", {"state": "ready", "manifest": build(minutes())})
    assert original["daily_pool_snapshots"][req["strategy_id"]]["symbols"] == ["600999.SH"]
    assert task["daily_pool_snapshots"][req["strategy_id"]]["symbols"] == req["pool"]["symbols"]
    assert task["strategy_input_manifests"][req["strategy_id"]]["stocks"]["input_sha256"]


def test_different_algorithm_does_not_bind_or_fallback():
    from app.services.strategy_input_binding import accept
    req = request()
    req["algorithm_sha256"] = "f" * 64
    task = {"daily_pool_snapshots": {}, "selection_snapshots": {}, "strategy_input_manifests": {}}
    with pytest.raises(ValueError, match="身份"):
        accept(task, req, "stocks", {"state": "ready", "manifest": build(minutes())})
    assert not task["strategy_input_manifests"]


def test_frozen_breadth_compute_does_not_register_unrelated_market_views(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.strategy_input_binding.algorithm_sha256", lambda: "b" * 64)
    from datetime import timedelta

    from test_strategy_input_manifest import AT

    from app.market_time import CN_TZ
    from app.multitenancy import compute_worker, monitor_compute
    from app.tickflow.repository import DataStore
    now = (AT + timedelta(seconds=40)).replace(tzinfo=CN_TZ)
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: now)
    monkeypatch.setattr("app.market_time.cn_now", lambda: now)
    monkeypatch.setattr(DataStore, "__init__", lambda *_a, **_kw:
                        pytest.fail("frozen S02 registered global market SQL views"))
    req = request()
    sid = req["strategy_id"]
    signal = {**req["signal"], "template": "late_session_strength",
              "params": {**req["signal"]["params"], "pool_up_min_pct": None,
                         "evaluation_delay_minutes": .5}}
    req["signal"] = signal
    task = {
        "kind": "monitor_breadth", "monitor_phase": "breadth", "s02_inputs_required": True,
        "strategy_task_ids": [sid], "state": {"engine": {}}, "sell_rosters": {}, "rules": [],
        "strategy_task_plans": {sid: {"actions": [{
            "kind": "buy", "signal_id": signal["id"], "phase": "breadth",
            "scheduled_at": now.isoformat(), "retry_until": now.replace(minute=55, second=0).isoformat()}]}},
        "strategy_task_windows": {sid: {"buy": [{"signal": signal, "start": "14:30:00",
            "end": "14:55:00", "phase_policy": "independent_v1"}], "sell": []}},
        "strategy_task_receipts": {sid: {}}, "strategy_task_results": {},
        "daily_pool_snapshots": {sid: req["pool"]}, "daily_identities": {sid: {
            "asset_type": "stock", "timeframe": "1m", "runtime_identity": sid}},
        "selection_snapshots": {sid: req["selection"]}, "late_task_scopes": {sid: sid},
        "strategy_input_manifests": {sid: {"breadth": build(minutes(), phase="breadth", value=req)}},
    }
    result = compute_worker.execute_request(task, tmp_path)
    saved = result["results"][sid]["monitor_tasks"]["buy"]["signals"][signal["id"]]["breadth"]
    assert saved["terminal"] and saved["state"] == "success"
    assert saved["pool_total"] == saved["pool_usable"] == 2


def test_frozen_compute_rejects_another_loaded_algorithm(monkeypatch):
    from app.services.strategy_input_binding import frozen_context
    monkeypatch.setattr("app.services.strategy_input_binding.algorithm_sha256", lambda: "f" * 64)
    with pytest.raises(ValueError, match="算法"):
        frozen_context(build(minutes()))


def test_algorithm_identity_changes_with_numerical_dependency(monkeypatch):
    import polars as pl

    from app.services.strategy_input_binding import algorithm_sha256
    algorithm_sha256.cache_clear()
    try:
        original = algorithm_sha256()
        monkeypatch.setattr(pl, "__version__", "different-test-version")
        algorithm_sha256.cache_clear()
        assert algorithm_sha256() != original
    finally:
        algorithm_sha256.cache_clear()
