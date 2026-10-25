"""Post-close schedules persist exactly and update the running scheduler."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.config import settings
from app.jobs import daily_pipeline
from app.services import preferences, trading_day
from app.tickflow.capabilities import CapabilitySet

CN = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    path = tmp_path / "preferences.json"
    monkeypatch.setattr(preferences, "_path", lambda: path)
    preferences._invalidate_cache()
    yield path
    preferences._invalidate_cache()


def test_pipeline_defaults_to_1600_without_writing_preferences(isolated_preferences):
    assert preferences.get_pipeline_schedule() == {"hour": 16, "minute": 0}
    assert not isolated_preferences.exists()


@pytest.mark.parametrize("schedule", [
    {"hour": 15, "minute": 35},
    {"hour": 16, "minute": 0},
    {"hour": 17, "minute": 42},
    {"hour": 23, "minute": 59},
])
def test_saved_schedule_is_exact_and_survives_restart(schedule):
    assert preferences.set_pipeline_schedule(**schedule) == schedule
    preferences._invalidate_cache()
    assert preferences.get_pipeline_schedule() == schedule


@pytest.mark.parametrize("schedule", [
    {"hour": 15, "minute": 34},
    {"hour": 9, "minute": 0},
    {"hour": -1, "minute": 0},
    {"hour": 24, "minute": 0},
    {"hour": 16, "minute": -1},
    {"hour": 16, "minute": 60},
])
def test_invalid_time_returns_error_without_overwriting_saved_schedule(schedule):
    saved = {"hour": 17, "minute": 42}
    preferences.set_pipeline_schedule(**saved)
    rescheduled = []
    scheduler = SimpleNamespace(reschedule_job=lambda *_args, **_kwargs: rescheduled.append(True))
    app = FastAPI()
    app.state.scheduler = scheduler
    app.include_router(settings_api.router)
    with TestClient(app) as client:
        response = client.put("/api/settings/preferences/pipeline-schedule", json=schedule)
    assert response.status_code == 422, response.text
    assert preferences.get_pipeline_schedule() == saved
    assert rescheduled == []


@pytest.mark.parametrize("automatic", [False, True])
def test_save_updates_real_trigger_and_restart_preserves_time_and_mode(monkeypatch, automatic):
    async def catalog(*_args, **_kwargs):
        pass

    monkeypatch.setattr(daily_pipeline, "run_catalog_job", catalog)
    preferences.save({"data_sync_auto_enabled": automatic})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    saved = {"hour": 17, "minute": 42}

    async def exercise():
        for restarting in (False, True):
            scheduler = daily_pipeline.start_scheduler(object(), CapabilitySet({}))
            request.app.state.scheduler = scheduler
            try:
                if not restarting:
                    assert settings_api.update_pipeline_schedule(
                        settings_api.PipelineScheduleIn(**saved), request,
                    ) == saved
                job = scheduler.get_job("daily_pipeline")
                next_run = job.trigger.get_next_fire_time(
                    None, datetime(2026, 10, 23, 16, tzinfo=CN),
                )
                assert (next_run.hour, next_run.minute) == (17, 42)
                assert (job.next_run_time is not None) is automatic
                assert preferences.get_pipeline_schedule() == saved
            finally:
                scheduler.shutdown(wait=False)
                await daily_pipeline.stop_catalog_jobs()
                await asyncio.sleep(0)
                preferences._invalidate_cache()

    asyncio.run(exercise())


@pytest.mark.parametrize("trading", [True, False, None])
def test_scheduled_pipeline_requires_trading_day_evidence(monkeypatch, trading):
    now = datetime(2026, 10, 1, 16, tzinfo=CN)
    checked, executed = [], []
    monkeypatch.setattr(daily_pipeline, "cn_now", lambda: now)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda day: checked.append(day) or trading)
    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr(daily_pipeline, "_run_tracked", lambda *_args: executed.append(True) or True)

    daily_pipeline._scheduled_pipeline_task(lambda: None)

    assert checked == [now]
    assert executed == ([True] if trading is True else [])
