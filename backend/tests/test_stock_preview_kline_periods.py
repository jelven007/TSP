from datetime import datetime

import polars as pl
import pytest
from fastapi.testclient import TestClient
from test_kline_periods_api import app as app

from app.api import kline
from app.services import kline_periods


@pytest.mark.parametrize("period", ["1m", "5m", "10m", "15m", "1w"])
def test_chart_query_returns_requested_period_across_days_in_order(app, monkeypatch, period):
    column = "date" if period == "1w" else "datetime"
    stamps = [datetime(2026, 10, 22, 14, 50), datetime(2026, 10, 23, 9, 40)]
    values = [stamp.date() if column == "date" else stamp for stamp in stamps]
    frame = pl.DataFrame({
        "symbol": ["600000.SH"] * 2, column: values,
        "open": [10., 11.], "high": [11., 12.], "low": [9., 10.],
        "close": [10.5, 11.5], "volume": [100., 200.], "amount": [1000., 2000.],
    })
    kline_periods.persist_period(app.state.repo, period, frame)
    monkeypatch.setattr(kline, "_get_stock_info", lambda *a: {"name": "测试"})
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("本地命中不得请求远程"))
    with TestClient(app) as client:
        response = client.get("/api/kline/daily", params={
            "symbol": "600000.SH", "freq": period,
            "start_date": "2026-10-19", "end_date": "2026-10-23",
        })
    assert response.status_code == 200, response.text
    rows = response.json()["rows"]
    assert response.json()["source"] == "local"
    assert rows[-1]["close"] == 11.5
    if period != "1w":
        assert len(rows) == 2
        assert rows[0]["date"].startswith("2026-10-22")
        assert rows[1]["date"].startswith("2026-10-23 09:40")


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_missing_derived_minute_data_is_empty_without_remote_or_other_period_substitution(app, monkeypatch, period):
    monkeypatch.setattr(kline, "_get_stock_info", lambda *a: {})
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("派生分钟不得远程取数"))
    with TestClient(app) as client:
        response = client.get("/api/kline/daily", params={
            "symbol": "600000.SH", "freq": period, "start_date": "2026-10-19", "end_date": "2026-10-23",
        })
    assert response.status_code == 200, response.text
    assert response.json()["rows"] == []
    assert response.json()["source"] == "none"


@pytest.mark.parametrize(("period", "count"), [("5m", 6), ("10m", 4), ("15m", 2)])
def test_chart_computes_derived_period_from_local_one_minute_across_days(app, monkeypatch, period, count):
    stamps = [datetime(2026, 10, day, 9, minute) for day in (22, 23) for minute in (31, 40, 41)]
    frame = pl.DataFrame({
        "symbol": ["600000.SH"] * 6, "datetime": stamps,
        **{c: [10., 11., 12., 20., 21., 22.] for c in ("open", "high", "low", "close", "volume", "amount")},
    })
    kline_periods.persist_period(app.state.repo, "1m", frame)
    monkeypatch.setattr(kline, "_get_stock_info", lambda *a: {})
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("chart must stay local"))
    with TestClient(app) as client:
        response = client.get("/api/kline/daily", params={
            "symbol": "600000.SH", "freq": period, "start_date": "2026-10-22", "end_date": "2026-10-23",
        })
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "local"
    rows = response.json()["rows"]
    assert len(rows) == count
    assert rows[-1]["close"] == 22.
    assert rows[-1]["date"].startswith("2026-10-23")


def test_chart_period_query_keeps_missing_prices_and_rejects_invalid_range(app, monkeypatch):
    monkeypatch.setattr(kline, "_get_stock_info", lambda *a: {})
    frame = pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [datetime(2026, 10, 23, 9, 40)],
        "open": [10.], "high": [None], "low": [9.], "close": [None], "volume": [None],
    })
    kline_periods.persist_period(app.state.repo, "10m", frame)
    with TestClient(app) as client:
        params = {"symbol": "600000.SH", "freq": "10m", "start_date": "2026-10-19", "end_date": "2026-10-23"}
        response = client.get("/api/kline/daily", params=params)
        assert response.status_code == 200
        assert response.json()["rows"][0]["close"] is None
        assert client.get("/api/kline/daily", params={**params, "freq": "invalid"}).status_code == 422
        assert client.get("/api/kline/daily", params={**params, "start_date": "2026-10-24"}).status_code == 422
