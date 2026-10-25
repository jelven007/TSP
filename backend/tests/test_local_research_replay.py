from urllib.parse import urlencode

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_original_snapshot_reaches_worker_and_new_experiment_is_not_cached(tmp_path, monkeypatch):
    from app.api import backtest
    from app.config import settings

    tasks = []
    def run(task, progress, cancel):
        tasks.append(task)
        return {"run_id": str(len(tasks)), "config": task["config"]}

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    monkeypatch.setattr("app.backtest.worker.run_worker_task", run)
    monkeypatch.setattr(backtest, "_running_jobs", {})
    app = FastAPI()
    app.include_router(backtest.router)
    client = TestClient(app)
    params = {"strategy_id": "s", "start": "2026-10-01", "end": "2026-10-20",
              "input_snapshot_id": "a" * 64, "experiment_id": "b" * 32}
    first = client.get("/api/backtest/strategy/stream", params=params)
    assert first.status_code == 200 and "event: done" in first.text
    assert tasks[0]["input_snapshot_id"] == "a" * 64
    assert "experiment_id" not in tasks[0]["config"]
    client.get("/api/backtest/strategy/stream", params=params)
    assert len(tasks) == 1  # reconnect uses the same experiment
    client.get("/api/backtest/strategy/stream", params={**params, "experiment_id": "c" * 32})
    assert len(tasks) == 2
    # Cancel resolves the new experiment key rather than a cached ordinary run.
    job = next(item for item in backtest._running_jobs.values() if item.result["run_id"] == "2")
    job.done = False
    response = client.post("/api/backtest/strategy/cancel", json={
        "qs": urlencode({**params, "experiment_id": "c" * 32}),
    })
    assert response.json()["ok"] and job.cancel_event.is_set()
    assert client.get("/api/backtest/strategy/stream", params={
        **params, "input_snapshot_id": "../escape",
    }).status_code == 400
