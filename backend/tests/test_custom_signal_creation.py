"""Creating signals must never replace an existing definition."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import signals
from app.strategy import custom_signals


def definition(signal_id="morning_liquidation", **overrides):
    return {
        "id": signal_id, "name": "Existing signal", "kind": "exit", "enabled": False,
        "timeframe": "intraday", "template": "morning_liquidation", "conditions": [],
        "params": {"time": "10:30:00", "fallback_time": "11:00:00", "rise_pct": 8, "fall_pct": 4},
        **overrides,
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(signals.router)
    invalidations = []
    monkeypatch.setattr(signals, "_invalidate", lambda request: invalidations.append(True))
    with TestClient(app) as client:
        yield client, tmp_path, invalidations


def test_create_conflict_preserves_original_and_does_not_invalidate(client):
    api, data_dir, invalidations = client
    original = definition()
    custom_signals.save_one(data_dir, original)
    before = (data_dir / "user_data/custom_signals/morning_liquidation.json").read_bytes()
    response = api.post("/api/custom-signals?create_only=true",
                        json=definition(name="New signal", enabled=True))
    assert response.status_code == 409, response.text
    assert "已存在" in response.json()["detail"]
    assert (data_dir / "user_data/custom_signals/morning_liquidation.json").read_bytes() == before
    assert api.get("/api/custom-signals").json()["signals"] == [original]
    assert invalidations == []


def test_create_new_id_and_legacy_edit_remain_distinct(client):
    api, data_dir, invalidations = client
    original = definition()
    custom_signals.save_one(data_dir, original)
    created = api.post("/api/custom-signals?create_only=true", json=definition("morning_copy"))
    assert created.status_code == 200, created.text
    assert len(api.get("/api/custom-signals").json()["signals"]) == 2
    changed = api.post("/api/custom-signals", json=definition("morning_copy", name="Edited"))
    assert changed.status_code == 200, changed.text
    stored = {sig["id"]: sig for sig in api.get("/api/custom-signals").json()["signals"]}
    assert stored[original["id"]] == original
    assert stored["morning_copy"]["name"] == "Edited"
    assert len(stored) == 2
    assert len(invalidations) == 2
    assert "create_only" not in stored["morning_copy"]


def test_concurrent_creates_allow_exactly_one_winner(client):
    api, data_dir, invalidations = client
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda name: api.post("/api/custom-signals?create_only=true", json=definition(name=name)),
            ["First", "Second"],
        ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    winner = next(response.json()["signal"] for response in responses if response.status_code == 200)
    assert custom_signals.load_all(data_dir) == [winner]
    assert invalidations == [True]
    assert not list(data_dir.rglob("*.tmp"))


def test_failed_creation_publishes_no_partial_signal(client, monkeypatch):
    api, data_dir, invalidations = client
    real_write = Path.write_text

    def torn_write(path, text, *args, **kwargs):
        real_write(path, text[:8], *args, **kwargs)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", torn_write)
    with pytest.raises(OSError, match="disk full"):
        api.post("/api/custom-signals?create_only=true", json=definition())
    assert custom_signals.load_all(data_dir) == []
    assert not list(data_dir.rglob("*.tmp"))
    assert invalidations == []
