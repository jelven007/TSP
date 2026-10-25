import asyncio
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data, kline, settings
from app.services import kline_periods, pipeline_jobs, preferences
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    monkeypatch.setattr(kline, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"), raising=False)
    monkeypatch.setattr(pipeline_jobs, "job_store", kline.job_store)
    app = FastAPI()
    app.state.repo = KlineRepository(DataStore(tmp_path))
    app.state.capabilities = SimpleNamespace(has=lambda cap: True)
    app.state.indicators_ready = True
    app.include_router(settings.router)
    app.include_router(kline.router)
    app.include_router(data.router)
    yield app
    app.state.repo.db.close()


def test_period_config_validation_persistence_and_empty_schema(app):
    with TestClient(app) as client:
        configs = client.get("/api/settings/kline-sync").json()
        assert configs["15m"]["interval_seconds"] == 900
        config = {**configs["1w"], "enabled": False, "time": "16:30"}
        assert client.post("/api/settings/kline-sync/1w", json=config).status_code == 200
        assert client.get("/api/settings/kline-sync").json()["1w"] == config
        assert client.post("/api/settings/kline-sync/unknown", json=config).status_code == 422
        assert client.post("/api/settings/kline-sync/1w", json={**config, "time": "12:00"}).status_code == 422
        for table in ("5m", "10m", "15m", "weekly"):
            fields = client.get(f"/api/data/schema/{table}").json()
            assert {row["name"] for row in fields} >= {"symbol", "open", "high", "low", "close", "volume", "amount"}
        assert client.post("/api/kline/sync_period", json={"period": "../1m"}).status_code == 422
        assert client.post("/api/kline/sync_period", json={"period": "5m", "value": 0}).status_code == 422


@pytest.mark.asyncio
async def test_manual_ten_minute_job_has_real_rows_and_completion(app, monkeypatch):
    repo = app.state.repo
    now = datetime(2026, 10, 26, 9, 31)
    frame = pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [now],
        "open": [10.], "high": [11.], "low": [9.], "close": [10.5],
        "volume": [100.], "amount": [1000.],
    })
    kline_periods.persist_period(repo, "1m", frame)
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": ["600000.SH"]}))
    monkeypatch.setattr("app.market_time.cn_today", lambda: now.date())
    request = SimpleNamespace(app=app)
    result = await kline.sync_period_job(request, {"period": "10m", "value": 1, "unit": "month"})
    for _ in range(50):
        job = pipeline_jobs.job_store.get(result["job_id"])
        if job["status"] not in {"queued", "running"}:
            break
        await asyncio.sleep(0.01)
    assert job["status"] == "succeeded"
    assert job["result"]["rows"] == 1
    assert repo.execute_one("SELECT close FROM kline_10minute")[0] == 10.5
