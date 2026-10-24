"""初始化默认自动同步, 用户关闭后修改调度时间不能重新启用。"""
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


@pytest.mark.parametrize("stored_off", [False, True])
def test_scheduler_defaults_on_and_preserves_saved_off(monkeypatch, stored_off):
    if stored_off:
        preferences.save({"data_sync_auto_enabled": False})
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
            assert all(
                (jobs[job_id].next_run_time is None) is stored_off for job_id in data_ids
            )

            caps = CapabilitySet({Cap.DEPTH5_BATCH: CapabilityLimits()})
            request = SimpleNamespace(
                state=SimpleNamespace(),
                cookies={},
                app=SimpleNamespace(state=SimpleNamespace(scheduler=scheduler, capabilities=caps)),
            )
            assert settings_api.get_preferences(request)["data_sync_auto_enabled"] is not stored_off
            settings_api.update_pipeline_schedule(
                settings_api.PipelineScheduleIn(hour=16, minute=10), request,
            )
            settings_api.update_instruments_schedule(
                settings_api.PipelineScheduleIn(hour=9, minute=5), request,
            )
            settings_api.update_depth_finalize_time(
                settings_api.DepthFinalizeTimeIn(hour=16, minute=5), request,
            )
            assert all(
                (scheduler.get_job(job_id).next_run_time is None) is stored_off
                for job_id in data_ids
            )

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

    preferences.save({"data_sync_auto_enabled": False})
    def unexpected(*args, **kwargs):
        pytest.fail("manual startup must not scan for automatic repair")

    monkeypatch.setattr(data_integrity, "scan_recent_integrity", unexpected)
    monkeypatch.setattr(data_integrity, "launch_integrity_repair", unexpected)
    data_integrity.boot_integrity_check(SimpleNamespace(repo=object()))


def test_manual_depth_boot_restores_cache_without_fetching(monkeypatch):
    from app.services.depth_service import DepthService

    preferences.save({"data_sync_auto_enabled": False})
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


def test_default_boot_scans_and_repairs_recent_integrity_issues(tmp_path, monkeypatch):
    from app.services import data_integrity

    day = datetime(2026, 10, 23).date()
    state = SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    scans, repairs = [], []
    monkeypatch.setattr(
        data_integrity, "scan_recent_integrity",
        lambda root: scans.append(root) or ["issue"],
    )
    monkeypatch.setattr(data_integrity, "earliest_issue_day", lambda issues: day)
    monkeypatch.setattr(data_integrity, "describe_issues", lambda issues: "missing data")
    monkeypatch.setattr(data_integrity, "within_auto_repair_window", lambda earliest: True)
    monkeypatch.setattr(
        data_integrity, "launch_integrity_repair",
        lambda *args: repairs.append(args),
    )
    data_integrity.boot_integrity_check(state)
    assert scans == [tmp_path]
    assert repairs == [(state, day, "boot_integrity_check")]


@pytest.mark.parametrize("capable,after_close", [(True, True), (False, True), (True, False)])
def test_default_depth_boot_obeys_capability_and_time_gate(monkeypatch, capable, after_close):
    from app.services.depth_service import DepthService

    service = DepthService()
    monkeypatch.setattr(service, "_has_capability", lambda: capable)
    monkeypatch.setattr(service, "_can_finalize", lambda: after_close)
    monkeypatch.setattr(service, "_persisted_for_date", lambda day: False)
    calls = []
    monkeypatch.setattr(service, "finalize", lambda: calls.append("finalized"))
    service.boot_check()
    assert calls == (["finalized"] if capable and after_close else [])
