"""初始化与调度配置修改均不得意外启动数据任务。"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from app.api import settings as settings_api
from app.jobs import daily_pipeline
from app.services import preferences
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()


def test_scheduler_starts_with_all_data_jobs_paused_including_auction_catchup(monkeypatch):
    monkeypatch.setattr(
        daily_pipeline, "cn_now",
        lambda: datetime(2026, 10, 23, 9, 25, 10, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    async def exercise():
        scheduler = daily_pipeline.start_scheduler(object(), CapabilitySet({}))
        try:
            jobs = {job.id: job for job in scheduler.get_jobs()}
            assert "auction_snapshot_catchup" in jobs
            assert jobs["reprobe_capabilities"].next_run_time is not None
            data_ids = set(jobs) - {"reprobe_capabilities"}
            assert {"pre_market_instruments", "daily_pipeline", "depth_finalize"} <= data_ids
            assert all(jobs[job_id].next_run_time is None for job_id in data_ids)

            caps = CapabilitySet({Cap.DEPTH5_BATCH: CapabilityLimits()})
            request = SimpleNamespace(
                state=SimpleNamespace(),
                cookies={},
                app=SimpleNamespace(state=SimpleNamespace(scheduler=scheduler, capabilities=caps)),
            )
            assert settings_api.get_preferences(request)["data_sync_auto_enabled"] is False
            settings_api.update_pipeline_schedule(
                settings_api.PipelineScheduleIn(hour=16, minute=10), request,
            )
            settings_api.update_instruments_schedule(
                settings_api.PipelineScheduleIn(hour=9, minute=5), request,
            )
            settings_api.update_depth_finalize_time(
                settings_api.DepthFinalizeTimeIn(hour=16, minute=5), request,
            )
            assert all(scheduler.get_job(job_id).next_run_time is None for job_id in data_ids)

            assert settings_api.update_data_sync_auto(
                settings_api.DataSyncAutoIn(enabled=True), request,
            ) == {"data_sync_auto_enabled": True}
            assert all(scheduler.get_job(job_id).next_run_time is not None for job_id in data_ids)
            assert preferences.get_data_sync_auto_enabled() is True
            settings_api.update_data_sync_auto(settings_api.DataSyncAutoIn(enabled=False), request)
            assert all(scheduler.get_job(job_id).next_run_time is None for job_id in data_ids)
            assert scheduler.get_job("reprobe_capabilities").next_run_time is not None
        finally:
            scheduler.shutdown(wait=False)
        await asyncio.sleep(0)

    asyncio.run(exercise())


def test_manual_boot_does_not_scan_or_launch_integrity_repair(monkeypatch):
    from app.services import data_integrity

    def unexpected(*args, **kwargs):
        pytest.fail("manual startup must not scan for automatic repair")

    monkeypatch.setattr(data_integrity, "scan_recent_integrity", unexpected)
    monkeypatch.setattr(data_integrity, "launch_integrity_repair", unexpected)
    data_integrity.boot_integrity_check(SimpleNamespace(repo=object()))


def test_manual_depth_boot_restores_cache_without_fetching(monkeypatch):
    from app.services.depth_service import DepthService

    service = DepthService()
    monkeypatch.setattr(service, "_has_capability", lambda: True)
    monkeypatch.setattr(service, "_can_finalize", lambda: True)
    monkeypatch.setattr(service, "_persisted_for_date", lambda day: False)

    def unexpected():
        pytest.fail("manual startup must not finalize depth")

    monkeypatch.setattr(service, "finalize", unexpected)
    service.boot_check()
    restored = []
    monkeypatch.setattr(service, "_persisted_for_date", lambda day: True)
    monkeypatch.setattr(service, "_restore_from_parquet", restored.append)
    service.boot_check()
    assert len(restored) == 1
