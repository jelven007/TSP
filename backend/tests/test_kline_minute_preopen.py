"""盘前分时显示最近已有交易日, 明确请求今天不能补拉尚未形成的分钟。"""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline as kline_api
from app.market_time import CN_TZ
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

TODAY = date(2026, 11, 25)
PREVIOUS = date(2026, 11, 24)
SYMBOL = "002452.SZ"


def _session():
    morning = datetime(2026, 11, 24, 9, 31)
    afternoon = datetime(2026, 11, 24, 13, 1)
    return pl.DataFrame({
        "datetime": [morning + timedelta(minutes=i) for i in range(120)]
        + [afternoon + timedelta(minutes=i) for i in range(120)],
        "close": [10.0] * 240,
    })


@pytest.fixture
def market(monkeypatch, tmp_path):
    now = datetime(2026, 11, 25, 7, 54, tzinfo=CN_TZ)
    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    monkeypatch.setattr(kline_api, "cn_now", lambda: now)
    monkeypatch.setattr(kline_api, "in_continuous_session", lambda: False)
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(kline_api, "_get_stock_info", lambda *args: {})
    monkeypatch.setattr(kline_api, "_get_previous_closes", lambda *args: {})
    monkeypatch.setattr(kline_api, "_get_price_limit_info", lambda *args: None)
    monkeypatch.setattr(kline_api, "_gzip_payload", lambda request, payload, **kwargs: payload)
    # A provider timeout must remain observable if the premature fetch is reintroduced.
    fetch = MagicMock(side_effect=TimeoutError("no completed minute before opening"))
    monkeypatch.setattr(kline_api.kline_sync, "fetch_minute_single", fetch)
    repo = MagicMock()
    repo.store = SimpleNamespace(data_dir=tmp_path)
    repo.resolve_asset_type.return_value = "stock"
    repo.latest_minute_date.return_value = PREVIOUS
    repo.latest_daily_date.return_value = PREVIOUS
    repo.get_minute.side_effect = (
        lambda symbol, day, **kwargs: _session() if day == PREVIOUS else pl.DataFrame()
    )
    app = FastAPI()
    app.include_router(kline_api.router)
    app.state.repo = repo
    app.state.capabilities = CapabilitySet({Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits()})
    return TestClient(app, raise_server_exceptions=False), repo, fetch


@pytest.mark.parametrize("live", [0, 1])
@pytest.mark.parametrize("verdict", [True, None])
@pytest.mark.parametrize(("hour", "minute"), [(7, 54), (9, 29)])
def test_preopen_default_serves_latest_available_session(
    market, monkeypatch, live, verdict, hour, minute,
):
    client, repo, fetch = market
    monkeypatch.setattr(kline_api.trading_day, "is_trading_day", lambda now=None: verdict)
    monkeypatch.setattr(
        kline_api, "cn_now", lambda: datetime(2026, 11, 25, hour, minute, tzinfo=CN_TZ),
    )
    response = client.get("/api/kline/minute", params={"symbol": SYMBOL, "live": live})

    assert response.status_code == 200
    body = response.json()
    assert (body["date"], body["source"], len(body["rows"])) == (str(PREVIOUS), "local", 240)
    repo.get_minute.assert_called_once_with(SYMBOL, PREVIOUS, asset_type="stock")
    fetch.assert_not_called()


@pytest.mark.parametrize("live", [0, 1])
def test_preopen_explicit_today_returns_empty_without_fetch(market, live):
    client, repo, fetch = market
    response = client.get(
        "/api/kline/minute", params={"symbol": SYMBOL, "date": str(TODAY), "live": live},
    )

    assert response.status_code == 200
    body = response.json()
    assert (body["date"], body["source"], body["rows"]) == (str(TODAY), "none", [])
    repo.get_minute.assert_called_once_with(SYMBOL, TODAY, asset_type="stock")
    repo.latest_minute_date.assert_not_called()
    fetch.assert_not_called()


def test_preopen_no_history_returns_empty_without_fetch(market):
    client, repo, fetch = market
    repo.latest_minute_date.return_value = None
    repo.latest_daily_date.return_value = None

    response = client.get("/api/kline/minute", params={"symbol": SYMBOL, "live": 1})

    assert response.status_code == 200
    body = response.json()
    assert (body["date"], body["source"], body["rows"]) == (str(TODAY), "none", [])
    fetch.assert_not_called()


def test_preopen_uses_daily_date_when_minute_date_missing(market):
    client, repo, fetch = market
    repo.latest_minute_date.return_value = None

    response = client.get("/api/kline/minute", params={"symbol": SYMBOL})

    assert response.status_code == 200
    body = response.json()
    assert (body["date"], len(body["rows"])) == (str(PREVIOUS), 240)
    fetch.assert_not_called()


def test_preopen_explicit_history_keeps_requested_date(market):
    client, repo, fetch = market

    response = client.get(
        "/api/kline/minute", params={"symbol": SYMBOL, "date": str(PREVIOUS), "live": 1},
    )

    assert response.status_code == 200
    assert response.json()["date"] == str(PREVIOUS)
    repo.latest_minute_date.assert_not_called()
    fetch.assert_not_called()
