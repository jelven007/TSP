import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.market_time import CN_TZ
from app.services import initial_sync, pipeline_jobs, post_close_sync


def test_startup_waits_for_catalog_without_blocking_and_shutdown_drains_worker(tmp_path, monkeypatch):
    catalog_done = asyncio.Event()
    entered, release = threading.Event(), threading.Event()
    jobs = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", jobs)
    monkeypatch.setattr(post_close_sync, "cn_now", lambda: datetime(2026, 11, 6, 10, tzinfo=CN_TZ))
    monkeypatch.setattr("app.market_time.cn_today", lambda: datetime(2026, 11, 6).date())
    monkeypatch.setattr(initial_sync.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr("app.config.settings.service_role", "platform")
    monkeypatch.setattr(post_close_sync.trading_day, "is_trading_day", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    pipeline_jobs.release_run_slot()
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: jobs.start(jid) or fn())

    async def catalog():
        await catalog_done.wait()

    monkeypatch.setattr("app.jobs.daily_pipeline.wait_for_catalog_jobs", catalog)
    calls = []

    def repair(*args, **kwargs):
        calls.append(kwargs["period"])
        entered.set()
        assert release.wait(3)
        kwargs["on_progress"]("sync", 50, "cancel checkpoint")
        pytest.fail("cancelled startup must stop before publishing")

    monkeypatch.setattr(post_close_sync, "run_repair_daily", repair)
    state = SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), capabilities="caps",
        quote_service=None,
    )

    async def exercise():
        await initial_sync.start_initial_sync(state)
        await initial_sync.start_initial_sync(state)
        await asyncio.sleep(0)
        assert not entered.is_set()
        catalog_done.set()
        assert await asyncio.to_thread(entered.wait, 1)
        shutdown = asyncio.create_task(initial_sync.stop_startup_sync(state))
        await asyncio.sleep(0)
        assert not shutdown.done()
        release.set()
        await shutdown
        assert calls == ["1m"]
        receipt = post_close_sync._load(state.repo, trigger="startup")
        assert receipt["status"] == "cancelled"
    asyncio.run(exercise())
    assert pipeline_jobs.try_acquire_run_slot("next")
    pipeline_jobs.release_run_slot()
