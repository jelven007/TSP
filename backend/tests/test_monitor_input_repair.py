"""Platform-owned repair is bounded by the same monitor generation and deadline."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_time import CN_TZ


def request():
    return {"owner": "tenant", "strategy_id": "custom_test", "generation": 1,
            "day": "2026-11-19", "phase": "breadth", "attempt": 1,
            "symbols": ["600001.SH"], "daily_dates": ["2026-11-18"],
            "minute_end": "14:30:00", "retry_until": "2026-11-19T14:30:30+08:00"}


def test_repair_bridge_authenticates_and_rejects_future_or_unbounded_input(monkeypatch):
    from app.cell.platform_market import router
    calls = []
    app = FastAPI()
    app.include_router(router)
    app.state.monitor_input_repair = SimpleNamespace(request=lambda value: calls.append(value) or {"state": "queued"})
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "repair-test")
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ))
    client = TestClient(app)
    path = "/internal/cell-market/monitor-repair"
    assert client.post(path, json=request()).status_code == 401
    headers = {"Authorization": "Bearer repair-test"}
    assert client.post(path, headers=headers, json=request()).status_code == 200
    assert len(calls) == 1
    assert client.post(path, headers=headers, json={**request(), "minute_end": "14:31:00"}).status_code == 422
    assert client.post(path, headers=headers, json={**request(), "day": "2026-11-20"}).status_code == 422


def test_selection_repair_respects_ordinary_and_s03_deadlines(monkeypatch):
    from app.cell.platform_market import router
    app = FastAPI()
    app.include_router(router)
    app.state.monitor_input_repair = SimpleNamespace(request=lambda value: {"state": "queued"})
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "repair-test")
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 11, 19, 20, 30, tzinfo=CN_TZ))
    client = TestClient(app)
    payload = {**request(), "phase": "select", "minute_end": None,
               "retry_until": "2026-11-19T22:00:00+08:00"}
    headers = {"Authorization": "Bearer repair-test"}
    path = "/internal/cell-market/monitor-repair"
    # 普通策略按当前 selection_clock 的22:00截止校验, 结束整秒允许修复。
    assert client.post(path, headers=headers, json=payload).status_code == 200
    assert client.post(path, headers=headers, json={
        **payload, "retry_until": "2026-11-19T22:00:01+08:00"}).status_code == 422
    assert client.post(path, headers=headers, json={
        **payload, "strategy_id": "custom_mvxxcs03",
        "retry_until": "2026-11-19T23:00:00+08:00"}).status_code == 200
    assert client.post(path, headers=headers, json={
        **payload, "strategy_id": "custom_mvxxcs03",
        "retry_until": "2026-11-19T23:00:01+08:00"}).status_code == 422


@pytest.mark.parametrize("cancelled", [False, True])
def test_repair_guard_prevents_late_or_cancelled_writes(tmp_path, cancelled):
    from app.services.monitor_input_repair import MonitorInputRepair
    clock = {"now": datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ)}
    service = MonitorInputRepair(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))),
                                 now=lambda: clock["now"])
    try:
        if cancelled:
            service.cancel("tenant", "custom_test", 2)
        else:
            clock["now"] = datetime(2026, 11, 19, 14, 30, 30, tzinfo=CN_TZ)
        with pytest.raises(ValueError):
            service.check(request())
    finally:
        service.close()


def test_sandbox_failure_retains_partial_breadth_and_retry_attempt(monkeypatch):
    from app.multitenancy import monitor_compute
    now = datetime(2026, 11, 19, 14, 30, 30, tzinfo=CN_TZ)
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: now)
    action = {"kind": "buy", "signal_id": "late", "phase": "stocks",
              "scheduled_at": now.isoformat(), "retry_until": "2026-11-19T14:55:00+08:00"}
    breadth = {"state": "success", "terminal": True, "passed": True, "pool_total": 1}
    payload = {"state": {}, "rules": [{"strategy_id": "custom_test"}], "strategy_task_ids": ["custom_test"],
               "strategy_task_plans": {"custom_test": {"actions": [action]}},
               "strategy_task_windows": {"custom_test": {"buy": [{"signal": {"id": "late"}}]}},
               "strategy_task_receipts": {"custom_test": {"tasks": {"buy": {"signals": {"late": {"breadth": breadth}}}}}},
               "strategy_task_results": {}, "daily_identities": {"custom_test": {
                   "runtime_identity": "def", "asset_type": "stock", "timeframe": "1m"}},
               "selection_snapshots": {}, "sell_rosters": {}}
    def fail(*_a, **_k):
        raise RuntimeError("compute timeout, missing start minute")
    result = monitor_compute.evaluate_strategy_phases_serial(payload, None, SimpleNamespace(execute=fail))
    task = result["results"]["custom_test"]["monitor_tasks"]["buy"]["signals"]["late"]
    assert task["breadth"] == breadth
    assert task["stocks"]["attempts"] == 1
    assert task["stocks"]["terminal"] is False
    assert "compute timeout" in task["stocks"]["errors"][0]


@pytest.mark.parametrize("cancelled", [False, True])
def test_provider_returning_after_cancel_or_deadline_cannot_persist(tmp_path, monkeypatch, cancelled):
    import polars as pl

    from app.services import kline_periods
    from app.services.monitor_input_repair import MonitorInputRepair
    clock = {"now": datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ)}
    writes = []
    service = MonitorInputRepair(SimpleNamespace(repo=SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path))), now=lambda: clock["now"])
    monkeypatch.setattr(kline_periods, "require_period_provider", lambda *_: (object(), None))
    def fetch(*_args, **_kwargs):
        if cancelled:
            service.cancel("tenant", "custom_test", 2)
        else:
            clock["now"] = datetime(2026, 11, 19, 14, 30, 30, tzinfo=CN_TZ)
        return pl.DataFrame({"symbol": ["600001.SH"]})
    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    monkeypatch.setattr(kline_periods, "persist_period", lambda *_a, **_k: writes.append(True))
    try:
        with pytest.raises(ValueError):
            service.repair(request())
        assert writes == []
    finally:
        service.close()


def test_morning_sell_repair_publishes_before_returning(tmp_path, monkeypatch):
    import polars as pl

    from app.services import kline_periods
    from app.services.monitor_input_repair import MonitorInputRepair
    calls = []
    service = MonitorInputRepair(SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        public_publisher=SimpleNamespace(
            publish=lambda: calls.append("publish") or "release",
            publish_priority=lambda *_: pytest.fail("morning repair cannot use afternoon-only publish"))),
        now=lambda: datetime(2026, 11, 19, 10, 0, tzinfo=CN_TZ))
    monkeypatch.setattr(kline_periods, "require_period_provider", lambda *_: (object(), None))
    monkeypatch.setattr(kline_periods, "fetch_period", lambda *_a, **_k: pl.DataFrame({"symbol": ["600001.SH"]}))
    monkeypatch.setattr(kline_periods, "persist_period", lambda *_a, **_k: calls.append("persist"))
    monkeypatch.setattr(kline_periods, "refresh_period", lambda *_: calls.append("refresh"))
    try:
        result = service.repair({**request(), "phase": "observe", "daily_dates": [],
            "minute_end": "10:00:00", "retry_until": "2026-11-19T11:30:00+08:00"})
        assert calls == ["persist", "refresh", "publish"]
        assert result["release"] == "release"
    finally:
        service.close()


def test_selection_repair_uses_complete_strategy_scope_and_declared_history(tmp_path, monkeypatch):
    import polars as pl
    from test_minute_signal_sections import CODE

    from app.services.strategy_monitor_tasks import selection_repairs
    from app.strategy.engine import StrategyEngine
    (tmp_path / "custom.py").write_text(CODE)
    engine = StrategyEngine([tmp_path])
    monkeypatch.setattr(engine, "minute_daily_history_bars", lambda *_a, **_k: 3)
    repo = SimpleNamespace(get_instruments_asset=lambda _: pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "300001.SZ"], "name": ["A", "B", "C"]}))
    calls = []
    repairs = selection_repairs(SimpleNamespace(
        monitor_repair=lambda req: calls.append(req) or {"state": "queued"}),
        repo, "tenant", {"custom_test": 1}, {
            "source_date": "2026-11-19", "attempts": 1}, {"custom_test": "missing daily"},
        engine=engine, storage=tmp_path)
    assert repairs["custom_test"]["state"] == "queued"
    assert calls[0]["symbols"] == ["600001.SH", "600002.SH"]
    assert calls[0]["daily_dates"] == ["2026-11-17", "2026-11-18", "2026-11-19"]
    assert calls[0]["minute_end"] is None


def test_narrowed_missing_group_reuses_covering_inflight_repair(tmp_path, monkeypatch):
    from app.services.monitor_input_repair import MonitorInputRepair
    service = MonitorInputRepair(
        SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))),
        now=lambda: datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ))
    submitted = []
    monkeypatch.setattr(service.pool, "submit", lambda *_: submitted.append(True))
    initial = {**request(), "phase": "stocks", "symbols": ["600001.SH", "600002.SH"],
               "daily_dates": ["2026-11-17", "2026-11-18"], "minute_start": "13:51:00",
               "certify_coverage": True}
    try:
        first = service.request(initial)
        narrowed = service.request({**initial, "symbols": ["600002.SH"],
                                    "daily_dates": ["2026-11-18"], "minute_start": "14:00:00",
                                    "attempt": 2})
        assert narrowed["id"] == first["id"]
        assert len(submitted) == 1
        foreign = service.request({**initial, "owner": "other"})
        assert foreign["id"] != first["id"] and len(submitted) == 2
    finally:
        service.close()


def test_retry_attempts_share_one_inflight_gap_and_keep_success_proofs(tmp_path, monkeypatch):
    import threading

    from app.services import pipeline_jobs
    from app.services.monitor_input_repair import MonitorInputRepair
    gate = threading.Event()
    started = threading.Event()
    service = MonitorInputRepair(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))),
                                 now=lambda: datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ))
    monkeypatch.setattr(pipeline_jobs, "try_acquire_run_slot", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "release_run_slot", lambda _: None)
    def repair(_):
        started.set()
        assert gate.wait(3)
        return {"proof": {"600001.SH": {"minute": [{"request_seq": 7}]}}}
    monkeypatch.setattr(service, "repair", repair)
    try:
        first = service.request(request())
        assert started.wait(1)
        second = service.request({**request(), "attempt": 9})
        assert second["id"] == first["id"]
        assert len(service.active) == 1
    finally:
        gate.set()
        service.pool.shutdown(wait=True)
        service.close()
    saved = service._read(first["id"])
    assert saved["state"] == "succeeded"
    assert saved["result"]["proof"]["600001.SH"]["minute"][0]["request_seq"] == 7


def test_history_coverage_preserves_two_successes_for_every_missing_day(tmp_path, monkeypatch):
    from app.services import pipeline_jobs
    from app.services.monitor_input_repair import MonitorInputRepair
    service = MonitorInputRepair(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))),
                                 now=lambda: datetime(2026, 11, 19, 14, 30, 15, tzinfo=CN_TZ))
    monkeypatch.setattr(pipeline_jobs, "try_acquire_run_slot", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "release_run_slot", lambda _: None)
    req = {**request(), "phase": "stocks", "certify_coverage": True}
    days = ["2026-11-16", "2026-11-17", "2026-11-18"]
    def repair(_):
        return {"proof": {"600001.SH": {"daily": [
            {"day": day, "request_seq": 100 + i} for i, day in enumerate(days)]}}}
    monkeypatch.setattr(service, "repair", repair)
    value = {"id": "test", "request": req, "successes": 1, "result": {"proof": {
        "600001.SH": {"daily": [{"day": day, "request_seq": 90 + i} for i, day in enumerate(days)]}}}}
    try:
        service._run("test", value)
        saved = service._read("test")
        proof = saved["result"]["proof"]["600001.SH"]["daily"]
        assert len(proof) == 6
        assert all(sum(row["day"] == day for row in proof) == 2 for day in days)
    finally:
        service.close()
