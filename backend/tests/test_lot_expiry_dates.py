"""Holding expiry shortcuts use the published A-share calendar."""
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import lots as lots_api
from app.strategy import lots as lots_domain
from app.strategy import monitor_rules


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(lots_api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), resolve_asset_type=lambda _: "stock",
    )
    monkeypatch.setattr(lots_api, "_reload_engine", lambda _: None)
    monkeypatch.setattr("app.services.preferences.get_webhook_default_channels", lambda: [])
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(("base", "next_day", "fifth_day"), [
    ("2026-09-30", "2026-10-08", "2026-10-14"),
    ("2026-10-30", "2026-11-02", "2026-11-06"),
    ("2026-11-01", "2026-11-02", "2026-11-06"),
    ("2026-11-02", "2026-11-03", "2026-11-09"),
    ("2025-12-31", "2026-01-05", "2026-01-09"),
    ("2026-12-30", "2026-12-31", None),
    ("2026-12-31", None, None),
    ("2027-01-04", None, None),
    ("9999-12-31", None, None),
])
def test_expiry_options_calendar_and_unknown_boundaries(client, base, next_day, fifth_day):
    response = client.get("/api/lots/expiry-options", params={"base_date": base})
    assert response.status_code == 200
    assert response.json() == {
        "base_date": base, "next_trading_day": next_day, "five_trading_days": fifth_day,
    }


def test_expiry_options_default_to_beijing_today(client, monkeypatch):
    monkeypatch.setattr("app.market_time.cn_today", lambda: date(2026, 11, 1))
    response = client.get("/api/lots/expiry-options")
    assert response.status_code == 200
    assert response.json() == {
        "base_date": "2026-11-01",
        "next_trading_day": "2026-11-02",
        "five_trading_days": "2026-11-06",
    }


@pytest.mark.parametrize("base", ["not-a-date", "2026-02-30"])
def test_expiry_options_reject_invalid_date(client, base):
    assert client.get("/api/lots/expiry-options", params={"base_date": base}).status_code == 422


def test_shortcut_date_propagates_to_batch_rules_and_can_be_edited_or_cleared(client, tmp_path):
    options = client.get("/api/lots/expiry-options", params={"base_date": "2026-09-30"})
    assert options.status_code == 200
    expiry = options.json()["five_trading_days"]
    response = client.post("/api/lots/batches", json={
        "name": "国庆提醒", "request_id": "expiry_test",
        "stocks": [
            {"symbol": symbol, "cost_price": 10, "target_pct": 6,
             "buy_date": "2026-09-30", "remind_date": expiry, "lead_days": 1}
            for symbol in ("600519.SH", "000001.SZ")
        ],
    })
    assert response.status_code == 200
    saved = response.json()
    for lot in saved["lots"]:
        rule = monitor_rules.load_one(tmp_path, f"{lot['id']}_d")
        assert rule["remind_date"] == lot["remind_date"] == "2026-10-14"
    lot = saved["lots"][0]
    for expiry in ("2026-10-17", None):  # Manual dates remain unrestricted and optional.
        edited = client.post("/api/lots", json={**lot, "remind_date": expiry})
        assert edited.status_code == 200
        assert edited.json()["lot"]["batch_id"] == saved["batch"]["id"]
        rule = monitor_rules.load_one(tmp_path, f"{lot['id']}_d")
        assert (rule["remind_date"] if rule else None) == expiry
    other = lots_domain.load_all(tmp_path)
    assert next(row for row in other if row["id"] != lot["id"])["remind_date"] == "2026-10-14"
