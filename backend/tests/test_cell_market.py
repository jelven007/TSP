"""Contract tests for the read-only shared market facade."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.cell.market import MarketQuote
from app.cell.platform_market import router
from app.market_time import cn_today


@pytest.fixture
def facade(monkeypatch):
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "test-platform")
    day = cn_today()
    quote_ts = datetime.now(UTC).timestamp() * 1000
    instrument = {"symbol": "000001.SZ", "as_of": day, "limit_up": 11., "limit_down": 9.}
    row = {"symbol": "000001.SZ", "date": day, "raw_close": 10., "change_pct": .01, "quote_ts": quote_ts}
    repo = SimpleNamespace(
        get_instruments=lambda: pl.DataFrame([instrument]),
        get_enriched_history=lambda as_of, bars: pl.DataFrame([row]),
    )
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    app.state.quote_service = SimpleNamespace(
        get_enriched_today=lambda: (pl.DataFrame([row]), day),
        status=lambda: {"is_trading_hours": True},
    )
    return TestClient(app), row, instrument, repo


def test_market_auth_symbols_timestamp_and_expiring_bands(facade):
    client, row, instrument, _ = facade
    url = "/internal/cell-market/quotes"
    auth = {"Authorization": "Bearer test-platform"}
    assert client.get(url, params={"symbols": "000001.SZ"}).status_code == 401
    assert client.get(url, params={"symbols": "688001.SH,689001.SH"}, headers=auth).status_code == 200
    for symbol in ("ABCDEF.SH", "430001.SH", "000001.SZ," * 1100):
        assert client.get(url, params={"symbols": symbol}, headers=auth).status_code == 422
    response = client.get(url, params={"symbols": "000001.SZ"}, headers=auth).json()["quotes"][0]
    assert response["sequence"] == int(row["quote_ts"])
    assert response["limits_known"]
    instrument["limit_up_as_of"] = cn_today() - timedelta(days=1)
    response = client.get(url, params={"symbols": "000001.SZ"}, headers=auth).json()["quotes"][0]
    assert not response["limits_known"]
    row["raw_close"] = float("nan")
    assert client.get(url, params={"symbols": "000001.SZ"}, headers=auth).json()["quotes"] == []


def test_market_history_nan_future_and_missing_as_of(facade):
    client, row, _, _ = facade
    params = {"symbols": "000001.SZ", "as_of": str(cn_today())}
    auth = {"Authorization": "Bearer test-platform"}
    row["ma60"] = float("nan")
    response = client.get("/internal/cell-market/history", params=params, headers=auth)
    assert response.status_code == 200
    assert response.json()["rows"][0]["ma60"] is None
    assert len(response.json()["release_id"]) == 64
    row["date"] = date(2020, 1, 1)
    assert client.get("/internal/cell-market/history", params=params, headers=auth).status_code == 503
    assert client.get("/internal/cell-market/history", params={**params, "as_of": str(cn_today() + timedelta(days=1))}, headers=auth).status_code == 422


def test_alert_freshness_is_independent_of_order_band_requirements():
    quote = MarketQuote(
        symbol="000001.SZ", price=10, change_pct=.01, source_time=datetime.now(UTC),
        sequence=1, trading=True, suspended=False, limits_known=False,
    )
    assert quote.fresh() and not quote.tradable()
    assert not quote.model_copy(update={"source_time": datetime.now(UTC) - timedelta(seconds=16)}).fresh()
    assert not quote.model_copy(update={"source_time": datetime.now(UTC) + timedelta(seconds=5)}).fresh()
