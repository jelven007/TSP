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

    def sync(data_dir, caps):
        worker_threads.append(threading.get_ident())
        assert data_dir == tmp_path and caps is scheduler._capset
        assert scheduler.is_syncing
        return 3

    monkeypatch.setattr(financial_sync.asyncio, "sleep", tick)
    monkeypatch.setattr(financial_sync, "sync_metrics", sync)
    monkeypatch.setattr(
        "app.services.data_release.publish_data_release",
        lambda *args, **kwargs: publications.append((args, kwargs)),
    )
    asyncio.run(scheduler._run_loop())

    assert len(worker_threads) == int(enabled)
    assert all(thread_id != main_thread for thread_id in worker_threads)
    assert publications == (
        [((tmp_path,), {
            "reason": "financial_sync:metrics",
            "write_lock": scheduler._write_lock,
        })] if enabled else []
    )
    assert ("metrics" in preferences.get_financial_sync_times()) is enabled
    assert scheduler.is_syncing is False
