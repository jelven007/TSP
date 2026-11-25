"""Morning scan publication and actual tenant pool/selection projection."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL and strategy fixture reuse.
from datetime import datetime
from types import SimpleNamespace

import pytest
from test_custom_status_refresh import DAY, identity, status
from test_full_daily_selections import source
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster
from test_full_schedules import claim, own

from app.cell.scheduler import Scheduler
from app.market_time import CN_TZ
from app.multitenancy.scheduled_worker import TenantSchedules
from app.services import announced_stock_status, preferences, related_market_data, suspension_sync
from app.services.daily_selections import DailySelections
from app.services.daily_stock_pools import DailyStockPools


def morning(slot="08:30"):
    return {**status(), "observed_at": f"{DAY}T{slot}:01+08:00",
            "scheduled_for": f"{DAY}T{slot}:00+08:00", "coverage_complete": False,
            "pending_count": 1, "errors": {"SZ": "分页未完成"}}


def test_platform_scans_again_at_0830_before_publishing_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(suspension_sync, "cn_now", lambda: datetime(2026, 11, 2, 8, 31, tzinfo=CN_TZ))
    monkeypatch.setattr(related_market_data, "cn_now", lambda: datetime(2026, 11, 2, 8, 31, tzinfo=CN_TZ))
    calls = []
    monkeypatch.setattr(suspension_sync, "request_sync",
                        lambda *a, **k: calls.append(("scan", k["refresh"])) or {"status": "failed"})
    monkeypatch.setattr(suspension_sync, "summary", lambda _: {
        "status": "failed", "cutoff": f"{DAY}T08:30:00+08:00",
        "coverage_complete": False, "pending_count": 1, "errors": {"SZ": "missing page"}})
    monkeypatch.setattr(announced_stock_status, "confirmed_status", lambda *a, **k: morning())
    scheduler = related_market_data.RelatedMarketDataScheduler()
    scheduler.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    scheduler.publisher = SimpleNamespace(publish=lambda: calls.append(("publish", True)))
    scheduler._run_scheduled(tmp_path, datetime(2026, 11, 2, 8, 29, tzinfo=CN_TZ))
    assert calls == []
    scheduler._run_scheduled(tmp_path, datetime(2026, 11, 2, 8, 30, tzinfo=CN_TZ))
    assert calls == [("scan", True), ("publish", True)]
    value = announced_stock_status.morning_status(
        scheduler.repo, DAY, observed_at=datetime(2026, 11, 2, 8, 31, tzinfo=CN_TZ), slot="08:30")
    assert value["pending_count"] == 1
    assert not value["coverage_complete"]
    scheduler._run_scheduled(tmp_path, datetime(2026, 11, 2, 8, 31, tzinfo=CN_TZ))
    assert calls == [("scan", True), ("publish", True), ("scan", False)]


@pytest.mark.parametrize("hour, minute, slot", [(8, 31, "08:30"), (9, 1, "09:00")])
def test_full_morning_prunes_current_selection_and_reprocesses_changed_status(
        env, tmp_path, monkeypatch, hour, minute, slot):
    _, documents = own(env)
    source(documents, tmp_path)
    DailySelections(documents).save(identity(), {"as_of": str(DAY), "source_date": "2026-10-30",
        "rows": [{"symbol": "600000.SH"}, {"symbol": "600001.SH"}, {"symbol": "600003.SH"}]}, started_at=1)
    state = morning(slot)
    monkeypatch.setattr(announced_stock_status, "morning_status", lambda *a, **k: state)
    calls = []

    def execute(task):
        calls.append(task)
        assert task["confirmed_status"]["target_date"] == str(DAY)
        return {"pools": {sid: {"strategy_id": sid, "as_of": str(DAY),
            "symbols": ["600000.SH", "600002.SH"], "total": 2}
            for sid in task["strategy_ids"]}, "errors": {}}

    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    lease = claim(env, documents)
    now = datetime(2026, 11, 2, hour, minute, tzinfo=CN_TZ)
    try:
        service.daily_pools(documents, {}, object(), now, lease)
        assert DailySelections(documents).read("custom_test", DAY)["rows"] == [{"symbol": "600000.SH"}]
        assert DailyStockPools(documents).read("custom_test", DAY)["generation_slot"] == slot
        assert DailyStockPools(documents).job(f"{DAY}:{slot}")["stock_status"]["pending_count"] == 1
        service.daily_pools(documents, {}, object(), now, lease)
        assert len(calls) == 1
        state["risk_warning"] = ["600000.SH", "600003.SH"]
        service.daily_pools(documents, {}, object(), now, lease)
        assert len(calls) == 2
        assert DailySelections(documents).read("custom_test", DAY)["rows"] == []
    finally:
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()


def test_morning_requires_current_batch_receipt(tmp_path):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(ValueError, match="本轮"):
        announced_stock_status.morning_status(
            repo, DAY, observed_at=datetime(2026, 11, 2, 8, 31, tzinfo=CN_TZ), slot="08:30")


def test_platform_failed_publication_does_not_leave_consumable_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(related_market_data, "cn_now",
                        lambda: datetime(2026, 11, 2, 8, 31, tzinfo=CN_TZ))
    monkeypatch.setattr(suspension_sync, "request_sync", lambda *a, **k: {"status": "succeeded"})
    monkeypatch.setattr(suspension_sync, "summary", lambda _: {
        "status": "succeeded", "cutoff": f"{DAY}T08:30:00+08:00", "coverage_complete": True})
    scheduler = related_market_data.RelatedMarketDataScheduler()
    scheduler.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    def failed():
        raise OSError("publication failure")
    scheduler.publisher = SimpleNamespace(publish=failed)
    with pytest.raises(OSError):
        scheduler._run_scheduled(tmp_path, datetime(2026, 11, 2, 8, 30, tzinfo=CN_TZ))
    assert not (tmp_path / announced_stock_status.MORNING_PATH).exists()


def test_full_late_first_round_cannot_overwrite_second_round(env, tmp_path, monkeypatch):
    from app.cell.context import AccessError
    _, documents = own(env)
    source(documents, tmp_path)
    monkeypatch.setattr(announced_stock_status, "morning_status", lambda *a, **k: morning())
    ticks = iter([0, 61])
    import time
    monkeypatch.setattr("app.multitenancy.scheduled_worker.time",
                        SimpleNamespace(monotonic=lambda: next(ticks), time_ns=time.time_ns))
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=lambda task: {
        "pools": {sid: {"strategy_id": sid, "as_of": str(DAY),
            "symbols": ["600000.SH"], "total": 1} for sid in task["strategy_ids"]}, "errors": {}}))
    lease = claim(env, documents)
    try:
        with pytest.raises(AccessError):
            service.daily_pools(documents, {}, object(),
                datetime(2026, 11, 2, 8, 59, tzinfo=CN_TZ), lease)
        assert DailyStockPools(documents).read("custom_test", DAY) is None
    finally:
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()


@pytest.mark.parametrize("hour, minute, slot", [(8, 31, "08:30"), (9, 1, "09:00")])
def test_local_morning_uses_real_complete_scope_and_prunes_selection(
        tmp_path, monkeypatch, hour, minute, slot):
    import polars as pl

    from app.services.daily_stock_pools import generate_scheduled_local
    from app.strategy.engine import StrategyEngine
    (tmp_path / "custom.py").write_text('''
import polars as pl
META = {"id": "custom_test", "name": "Test", "asset_types": ["stock"], "limit": 1}
BASIC_FILTER = {"target_mode": "selected", "boards": ["沪主板"],
                "exclude_st": True, "exclude_suspended": True, "price_max": 1}
def filter(df, params):
    return pl.lit(False)
''')
    engine = StrategyEngine([tmp_path])
    repo = SimpleNamespace(get_instruments_asset=lambda _: pl.DataFrame({
        "symbol": ["600000.SH", "600001.SH", "600002.SH", "600003.SH", "600005.SH", "000001.SZ"],
        "name": ["A", "B", "C", "D", "ST旧名", "范围外"],
        "is_risk_warning": [False, False, False, False, True, False]}))
    state = morning(slot)
    monkeypatch.setattr(announced_stock_status, "morning_status", lambda *a, **k: state)
    DailySelections(tmp_path).save(identity(), {"as_of": str(DAY), "source_date": "2026-10-30",
        "rows": [{"symbol": "600000.SH"}, {"symbol": "600001.SH"}, {"symbol": "600003.SH"}]}, started_at=1)
    result = generate_scheduled_local(engine, SimpleNamespace(repo=repo), tmp_path,
                                     now=datetime(2026, 11, 2, hour, minute, tzinfo=CN_TZ))
    assert result == {"saved": ["custom_test"], "errors": {}}
    assert DailyStockPools(tmp_path).read("custom_test", DAY)["symbols"] == [
        "600000.SH", "600002.SH", "600005.SH"]
    assert DailySelections(tmp_path).read("custom_test", DAY)["rows"] == [{"symbol": "600000.SH"}]
    assert DailyStockPools(tmp_path).read("custom_test", DAY)["generation_slot"] == slot


def test_second_scan_replaces_first_receipt_and_retries_new_coverage(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    stamp = datetime(2026, 11, 2, 8, 30, tzinfo=CN_TZ)
    monkeypatch.setattr(related_market_data, "cn_now", lambda: stamp)
    coverage = {"status": "succeeded", "cutoff": stamp.isoformat(),
                "coverage_complete": True, "pending_count": 0}
    calls = []
    monkeypatch.setattr(suspension_sync, "request_sync",
                        lambda *a, **k: calls.append(k["refresh"]) or {"status": "succeeded"})
    monkeypatch.setattr(suspension_sync, "summary", lambda _: coverage)
    scheduler = related_market_data.RelatedMarketDataScheduler()
    scheduler.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    scheduler.publisher = SimpleNamespace(publish=lambda: None)
    scheduler._run_scheduled(tmp_path, stamp)
    stamp = stamp.replace(hour=9, minute=0)
    coverage.update(cutoff=stamp.isoformat(), status="failed",
                    coverage_complete=False, pending_count=1)
    scheduler._run_scheduled(tmp_path, stamp)
    assert calls == [True, True]
    receipt = json.loads((tmp_path / announced_stock_status.MORNING_PATH).read_text())
    assert receipt["scheduled_for"] == f"{DAY}T09:00:00+08:00"
    assert receipt["pending_count"] == 1
    coverage.update(status="succeeded", coverage_complete=True, pending_count=0)
    stamp = stamp.replace(minute=1)
    scheduler._run_scheduled(tmp_path, stamp)
    assert calls == [True, True, False]
    assert announced_stock_status.morning_status(
        scheduler.repo, DAY, observed_at=stamp, slot="09:00")["coverage_complete"] is True
    scheduler._run_scheduled(tmp_path, stamp.replace(minute=2))
    assert calls == [True, True, False]


def test_second_consumer_rejects_first_receipt_and_accepts_published_second_late(tmp_path):
    import json
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    path = tmp_path / announced_stock_status.MORNING_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**morning(), "cutoff": f"{DAY}T08:30:00+08:00",
                               "finished_at": f"{DAY}T08:31:00+08:00", "status": "succeeded"}))
    with pytest.raises(ValueError, match="本轮"):
        announced_stock_status.morning_status(
            repo, DAY, observed_at=datetime(2026, 11, 2, 9, 1, tzinfo=CN_TZ), slot="09:00")
    path.write_text(json.dumps({**morning("09:00"), "cutoff": f"{DAY}T09:00:00+08:00",
                               "finished_at": f"{DAY}T09:01:00+08:00", "status": "succeeded"}))
    value = announced_stock_status.morning_status(
        repo, DAY, observed_at=datetime(2026, 11, 2, 10, tzinfo=CN_TZ), slot="09:00")
    assert value["scheduled_for"] == f"{DAY}T09:00:00+08:00"


@pytest.mark.parametrize("hour, minute, expected", [
    (8, 29, None), (8, 30, "08:30"), (8, 59, "08:30"),
    (9, 0, "09:00"), (9, 29, "09:00"), (9, 30, None), (20, 30, None),
])
def test_morning_scan_window_boundaries(hour, minute, expected):
    from app.services.daily_pool_schedule import morning_scan_slot
    assert morning_scan_slot(datetime(2026, 11, 2, hour, minute, tzinfo=CN_TZ)) == expected
    assert morning_scan_slot(datetime(2026, 11, 1, hour, minute, tzinfo=CN_TZ)) is None


def test_first_scan_finishing_in_second_round_is_not_published(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(related_market_data, "cn_now",
                        lambda: datetime(2026, 11, 2, 9, 1, tzinfo=CN_TZ))
    monkeypatch.setattr(suspension_sync, "request_sync", lambda *a, **k: {"status": "succeeded"})
    monkeypatch.setattr(suspension_sync, "summary", lambda _: {
        "status": "succeeded", "cutoff": f"{DAY}T08:59:00+08:00", "coverage_complete": True})
    scheduler = related_market_data.RelatedMarketDataScheduler()
    scheduler.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    scheduler.publisher = SimpleNamespace(publish=lambda: pytest.fail("stale first receipt published"))
    scheduler._run_scheduled(tmp_path, datetime(2026, 11, 2, 8, 59, tzinfo=CN_TZ))
    assert not (tmp_path / announced_stock_status.MORNING_PATH).exists()


def test_partial_morning_scan_retries_then_publishes_complete_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    stamp = datetime(2026, 11, 2, 8, 30, tzinfo=CN_TZ)
    monkeypatch.setattr(related_market_data, "cn_now", lambda: stamp)
    calls = []
    monkeypatch.setattr(suspension_sync, "request_sync",
                        lambda *a, **k: calls.append(k["refresh"]) or {"status": "succeeded"})
    coverage = {"status": "failed", "cutoff": stamp.isoformat(), "updated_at": stamp.isoformat(),
                "coverage_complete": False, "pending_count": 1}
    monkeypatch.setattr(suspension_sync, "summary", lambda _: coverage)
    scheduler = related_market_data.RelatedMarketDataScheduler()
    scheduler.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    scheduler.publisher = SimpleNamespace(publish=lambda: None)
    scheduler._run_scheduled(tmp_path, stamp)
    coverage.update(status="succeeded", coverage_complete=True, pending_count=0)
    scheduler._run_scheduled(tmp_path, stamp.replace(minute=31))
    assert calls == [True, False]
    receipt = __import__("json").loads((tmp_path / announced_stock_status.MORNING_PATH).read_text())
    assert receipt["coverage_complete"] is True
