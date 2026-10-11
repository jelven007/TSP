from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.second_quotes import router
from app.api.settings import router as settings_router
from app.backtest.second_replay import session_windows
from app.services import preferences
from app.services.quote_service import QuoteService
from app.services.second_quote_archive import build_snapshot_frame

DAY = date(2026, 1, 5)
START = session_windows(DAY)[0][0]
SYMBOL = "600519.SH"
BASE = {"start": str(DAY), "end": str(DAY), "symbols": [SYMBOL]}


@pytest.fixture
def client(tmp_path):
    app = FastAPI()
    app.include_router(router)
    app.include_router(settings_router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as result:
        yield result


def _write_data(tmp_path):
    frames = []
    for i in range(4):
        stamp = START + i * 1000
        frames.append(build_snapshot_frame({"stock": [{
            "symbol": SYMBOL, "last_price": 10.0, "volume": 1000.0 + i,
            "amount": 1_000_000.0 + i * 1000, "source_timestamp": stamp,
            "provider_version": "1.12.2@test",
        }]}, provider="rustdx", capture_id=stamp * 1_000_000,
            collected_at_ms=stamp, trade_date=DAY))
    path = tmp_path / "quote_second" / f"date={DAY}" / "hour=09" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.concat(frames).write_parquet(path)
    return path


def test_status_and_empty_window(client):
    assert client.get("/api/backtest/second-quotes/status").json()["available_dates"] == []
    response = client.post("/api/backtest/second-quotes/validate", json=BASE)
    assert response.status_code == 200
    assert response.json()["status"] == "missing"


def test_sample_preserves_large_ids_and_exact_truncation(client, tmp_path):
    _write_data(tmp_path)
    for limit, truncated in ((3, True), (4, False)):
        response = client.post("/api/backtest/second-quotes/sample", json={**BASE, "limit": limit})
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == limit
        assert data["truncated"] is truncated
        assert data["rows"][0]["capture_id"] == str(START * 1_000_000)
    assert client.get("/api/backtest/second-quotes/status").json()["available_dates"] == [str(DAY)]


def test_quality_and_execution_endpoints(client, tmp_path):
    _write_data(tmp_path)
    response = client.post("/api/backtest/second-quotes/validate", json={
        **BASE, "start_ms": START, "end_ms": START + 3000,
    })
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    response = client.post("/api/backtest/second-quotes/execution-validate", json={
        "day": str(DAY), "start_ms": START, "end_ms": START + 3000,
        "orders": [{"order_id": "one", "symbol": SYMBOL, "side": "buy", "shares": 100, "signal_ms": START}],
    })
    assert response.status_code == 200
    assert response.json()["orders"][0]["reason"] == "historical_status_unknown"


@pytest.mark.parametrize("endpoint,payload,status", [
    ("sample", {**BASE, "symbols": []}, 422),
    ("validate", {**BASE, "symbols": [""]}, 422),
    ("validate", {**BASE, "start_ms": START}, 422),
    ("sample", {**BASE, "end": "2026-03-01"}, 400),
    ("sample", {**BASE, "end": "2026-01-04"}, 422),
    ("execution-validate", {
        "day": str(DAY), "start_ms": START, "end_ms": START + 3000,
        "orders": [{"order_id": "one", "symbol": SYMBOL, "side": "buy", "shares": 100, "signal_ms": START}],
    }, 409),
])
def test_bad_requests_and_missing_execution_data(client, endpoint, payload, status):
    assert client.post(f"/api/backtest/second-quotes/{endpoint}", json=payload).status_code == status


def test_corrupt_shard_returns_conflict(client, tmp_path):
    _write_data(tmp_path).write_bytes(b"broken")
    assert client.post("/api/backtest/second-quotes/validate", json=BASE).status_code == 409


def test_wrong_column_type_returns_conflict(client, tmp_path):
    path = _write_data(tmp_path)
    pl.read_parquet(path).with_columns(pl.col("volume").cast(pl.String)).write_parquet(path)
    response = client.post("/api/backtest/second-quotes/validate", json=BASE)
    assert response.status_code == 409
    assert "invalid schema types" in response.json()["detail"]


def test_unavailable_repository_returns_service_unavailable(client):
    client.app.state.repo = None
    assert client.get("/api/backtest/second-quotes/status").status_code == 503


def test_preference_enable_disable_drains_archive(client, tmp_path, monkeypatch):
    saved = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(saved))
    monkeypatch.setattr(preferences, "save", lambda values: saved.update(values))
    service = QuoteService()
    service.set_repo(client.app.state.repo)
    client.app.state.quote_service = service
    path = "/api/settings/preferences/second-quote-archive"
    assert client.put(path, json={"second_quote_archive_enabled": True}).status_code == 200
    assert service._second_quote_archive.submit(
        {"stock": [{"symbol": SYMBOL, "last_price": 10}]}, provider="rustdx",
        collected_at_ms=START, trade_date=DAY,
    )
    response = client.put(path, json={"second_quote_archive_enabled": False})
    assert response.status_code == 200
    assert response.json()["status"]["written_rounds"] == 1
    assert response.json()["status"]["running"] is False
    assert saved["second_quote_archive_enabled"] is False
    assert len(list((tmp_path / "quote_second").rglob("*.parquet"))) == 1
    assert client.put(path, json={
        "second_quote_archive_enabled": True, "second_quote_archive_retention_days": -1,
    }).status_code == 422


def test_failed_reopen_leaves_preference_disabled(client, monkeypatch):
    saved = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(saved))
    monkeypatch.setattr(preferences, "save", lambda values: saved.update(values))

    def fail(_enabled):
        raise RuntimeError("still draining")

    client.app.state.quote_service = SimpleNamespace(configure_second_quote_archive=fail)
    response = client.put("/api/settings/preferences/second-quote-archive", json={
        "second_quote_archive_enabled": True,
    })
    assert response.status_code == 409
    assert saved["second_quote_archive_enabled"] is False
