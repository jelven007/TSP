"""财务默认周期任务复用线程、互斥与发布链路, 并遵守自动同步总开关。"""
import asyncio
import threading

import pytest

from app.services import financial_sync, preferences
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: False)
    yield
    preferences._invalidate_cache()


@pytest.mark.parametrize("auto_schedule", [None, False])
def test_financial_scheduler_defaults_on_and_preserves_explicit_disabled(
    tmp_path, monkeypatch, auto_schedule,
):
    scheduler = financial_sync.FinancialScheduler()
    caps = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    started = []

    async def record_loop():
        started.append(True)

    monkeypatch.setattr(scheduler, "_run_loop", record_loop)

    async def exercise():
        kwargs = {} if auto_schedule is None else {"auto_schedule": auto_schedule}
        scheduler.start(tmp_path, caps, **kwargs)
        await asyncio.sleep(0)
        assert started == ([True] if auto_schedule is None else [])
        scheduler.stop()

    asyncio.run(exercise())


@pytest.mark.parametrize("custom", [False, True])
def test_financial_scheduler_obeys_provider_capability(tmp_path, monkeypatch, custom):
    scheduler = financial_sync.FinancialScheduler()
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: custom)

    async def exercise():
        scheduler.start(tmp_path, CapabilitySet({}))
        assert (scheduler._task is not None) is custom
        scheduler.stop()
        await asyncio.sleep(0)

    asyncio.run(exercise())


@pytest.mark.parametrize("enabled", [False, True])
def test_financial_auto_sync_uses_worker_and_publishes_with_write_lock(
    tmp_path, monkeypatch, enabled,
):
    scheduler = financial_sync.FinancialScheduler()
    scheduler._data_dir = tmp_path
    scheduler._capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    scheduler._write_lock = threading.RLock()
    scheduler._running = True
    worker_threads, publications = [], []
    main_thread = threading.get_ident()
    sleeps = 0

    async def tick(seconds):
        nonlocal sleeps
        sleeps += 1
        # 总开关每轮读取, 保存关闭后不发起周期请求。
        if sleeps == 1:
            preferences.save({"data_sync_auto_enabled": enabled})
        else:
            scheduler._running = False

    def sync(table, symbols, data_dir, caps):
        worker_threads.append(threading.get_ident())
        assert data_dir == tmp_path and caps is scheduler._capset
        assert scheduler.is_syncing
        return 3

    monkeypatch.setattr(financial_sync.asyncio, "sleep", tick)
    monkeypatch.setattr(financial_sync, "_sync_history_table_for_symbols", sync)
    monkeypatch.setattr(
        "app.services.data_release.publish_data_release",
        lambda *args, **kwargs: publications.append((args, kwargs)),
    )
    asyncio.run(scheduler._run_loop())

    assert len(worker_threads) == len(financial_sync.FINANCIAL_TABLES) * int(enabled)
    assert all(thread_id != main_thread for thread_id in worker_threads)
    assert publications == (
        [((tmp_path,), {
            "reason": "financial_sync:all",
            "write_lock": scheduler._write_lock,
        })] if enabled else []
    )
    assert set(preferences.get_financial_sync_times()) == (
        set(financial_sync.FINANCIAL_TABLES) if enabled else set()
    )
    assert scheduler.is_syncing is False


def test_empty_fresh_fetch_does_not_rewrite_history_or_record_success(tmp_path, monkeypatch):
    import polars as pl

    directory = tmp_path / "financials/metrics"
    directory.mkdir(parents=True)
    path = directory / "part.parquet"
    pl.DataFrame({
        "symbol": ["600519.SH"], "period_end": ["2026-09-30"], "eps_basic": [10.0],
    }).write_parquet(path)
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(financial_sync, "_fetch_table", lambda *args, **kwargs: pl.DataFrame())
    with pytest.raises(RuntimeError, match="最新"):
        financial_sync._sync_history_table_for_symbols(
            "metrics", ["600519.SH"], tmp_path, CapabilitySet({}),
        )
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
