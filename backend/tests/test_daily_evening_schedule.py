"""Finance remains 21:00; related maintenance starts 20:30 in Beijing time."""
import asyncio
from datetime import datetime

import pytest

from app.market_time import CN_TZ
from app.services import financial_sync, preferences, related_market_data
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def now(day=19, hour=21, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=CN_TZ)


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    preferences.save({"data_sync_auto_enabled": True})
    yield
    preferences._invalidate_cache()


def finance(tmp_path):
    scheduler = financial_sync.FinancialScheduler()
    scheduler._data_dir = tmp_path
    scheduler._capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    return scheduler


def test_financial_runs_at_2100_once_across_restart_and_again_next_day(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(financial_sync.FinancialScheduler, "run_now",
                        lambda self: calls.append(True) or {table: 1 for table in financial_sync.FINANCIAL_TABLES})
    scheduler = finance(tmp_path)
    scheduler._run_scheduled(now(hour=20, minute=59))
    assert calls == []
    scheduler._run_scheduled(now())
    finance(tmp_path)._run_scheduled(now(hour=22))
    finance(tmp_path)._run_scheduled(now(day=20, hour=20))
    assert calls == [True]
    finance(tmp_path)._run_scheduled(now(day=20))
    assert calls == [True, True]


@pytest.mark.parametrize("result", [{}, {"_skipped": 1}])
def test_financial_skipped_run_does_not_consume_day(tmp_path, monkeypatch, result):
    scheduler = finance(tmp_path)
    calls = []
    results = iter([result, {table: 1 for table in financial_sync.FINANCIAL_TABLES}])
    monkeypatch.setattr(scheduler, "run_now", lambda: calls.append(True) or next(results))
    scheduler._run_scheduled(now())
    scheduler._run_scheduled(now(minute=1))
    scheduler._run_scheduled(now(minute=2))
    assert len(calls) == 2


def test_financial_auto_switch_leaves_manual_sync_available(tmp_path, monkeypatch):
    calls = []
    scheduler = finance(tmp_path)
    monkeypatch.setattr(scheduler, "run_now", lambda: calls.append(True) or {})
    preferences.save({"data_sync_auto_enabled": False})
    scheduler._run_scheduled(now())
    assert calls == []
    scheduler.run_now()
    assert calls == [True]


def test_financial_failure_is_recorded_without_repeating_daily_attempt(tmp_path, monkeypatch):
    scheduler = finance(tmp_path)
    calls = []
    def fail():
        calls.append(True)
        raise RuntimeError("upstream unavailable")
    monkeypatch.setattr(financial_sync.FinancialScheduler, "run_now", lambda self: fail())
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        scheduler._run_scheduled(now())
    finance(tmp_path)._run_scheduled(now(minute=1))
    assert len(calls) == 1
    assert scheduler._load_schedule()["status"] == "failed"


def test_related_maintenance_2030_checkpoint_freezes_date_across_midnight(tmp_path, monkeypatch):
    scheduler = related_market_data.RelatedMarketDataScheduler()
    calls = []
    def sync(data_dir, **kwargs):
        calls.append(kwargs)
        monkeypatch.setattr(related_market_data, "cn_today", lambda: now(day=20).date())
        return {"schema_version": 1, "datasets": {}}
    monkeypatch.setattr(related_market_data, "sync_related_data", sync)
    scheduler._run_scheduled(tmp_path, now(hour=18))
    scheduler._run_scheduled(tmp_path, now(hour=20, minute=29))
    assert calls == []
    scheduler._run_scheduled(tmp_path, now())
    related_market_data.RelatedMarketDataScheduler()._run_scheduled(tmp_path, now(hour=22))
    assert calls == [{"end": now().date()}]
    assert related_market_data._load_state(tmp_path)["maintenance_date"] == "2026-11-19"


def test_related_disabled_and_busy_do_not_consume_maintenance(tmp_path, monkeypatch):
    calls = []
    scheduler = related_market_data.RelatedMarketDataScheduler()
    results = iter([{"skipped": "already_running"}, {"schema_version": 1, "datasets": {}}])
    monkeypatch.setattr(related_market_data, "sync_related_data",
                        lambda *args, **kwargs: calls.append(True) or next(results))
    preferences.save({"data_sync_auto_enabled": False})
    scheduler._run_scheduled(tmp_path, now())
    assert calls == []
    preferences.save({"data_sync_auto_enabled": True})
    scheduler._run_scheduled(tmp_path, now())
    scheduler._run_scheduled(tmp_path, now(minute=1))
    scheduler._run_scheduled(tmp_path, now(minute=2))
    assert calls == [True, True]


def test_financial_loop_dispatches_fixed_clock_check_in_thread(tmp_path, monkeypatch):
    scheduler = finance(tmp_path)
    scheduler._running = True
    calls = []
    async def sleep(seconds):
        assert 0 < seconds <= 60
        if calls:
            scheduler._running = False
    monkeypatch.setattr(financial_sync.asyncio, "sleep", sleep)
    monkeypatch.setattr(financial_sync, "cn_now", lambda: now())
    monkeypatch.setattr(scheduler, "_run_scheduled", lambda stamp: calls.append(stamp))
    asyncio.run(scheduler._run_loop())
    assert calls == [now()]
