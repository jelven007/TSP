from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import market_time
from app.api import screener


class Repo:
    def __init__(self, frame, day):
        self.frame, self.day = frame, day
        self.calls = []
        self.warmups = 0

    def get_enriched_latest(self, *, refresh=True):
        self.calls.append(refresh)
        assert refresh is False, "one-second requests must not recompute history"
        return self.frame, self.day

    def ensure_enriched_warmup(self):
        self.warmups += 1


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr(market_time, "cn_today", lambda: date(2026, 11, 6))
    repo = Repo(pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"], "date": [date(2026, 11, 6)] * 2,
        "raw_close": [12.5, 20.0], "close": [6.25, 10.0], "change_pct": [.02, .03],
        "turnover_rate": [3.45, None], "vol_ratio_5d": [1.8, float("nan")],
        "quote_ts": [1793930400000, 1793930400000], "signal_ma_golden_5_20": [True, False],
    }), date(2026, 11, 6))
    app = FastAPI()
    app.include_router(screener.router)
    app.state.repo = repo
    monkeypatch.setattr(screener, "ScreenerService", lambda _: SimpleNamespace(
        latest_date=lambda: repo.day, _load_enriched_for_date=lambda _: repo.frame))
    return TestClient(app), repo


def test_realtime_snapshot_filters_cached_symbols_and_preserves_raw_price_units_and_signals(live):
    client, repo = live
    response = client.get("/api/screener/market-snapshot?realtime=true&symbols=600001.SH")
    assert response.status_code == 200
    payload = response.json()
    assert payload["as_of"] == "2026-11-06"
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["symbol"] == "600001.SH"
    assert row["raw_close"] == 12.5
    assert row["change_pct"] == .02
    assert row["turnover_rate"] == 3.45
    assert row["signal_ma_golden_5_20"] is True
    assert row["quote_ts"] == 1793930400000
    assert repo.calls == [False]


@pytest.mark.parametrize("cold", [False, True])
def test_no_current_cache_never_falls_back_to_saved_daily_prices(live, cold):
    client, repo = live
    repo.day = None if cold else date(2026, 11, 5)
    if cold:
        repo.frame = pl.DataFrame()
    payload = client.get("/api/screener/market-snapshot?realtime=true&symbols=600001.SH").json()
    assert payload["as_of"] == "2026-11-06"
    assert payload["rows"] == []
    assert repo.calls == [False]
    assert repo.warmups == int(cold)


@pytest.mark.parametrize("symbols", ["", "ABC", "600001.SH,ABC", ",".join(f"{i:06d}.SZ" for i in range(1001))])
def test_realtime_symbol_range_is_required_valid_and_bounded(live, symbols):
    client, repo = live
    response = client.get("/api/screener/market-snapshot", params={"realtime": "true", "symbols": symbols})
    assert response.status_code == 422
    assert repo.calls == []


def test_original_market_snapshot_keeps_its_full_market_contract(live, monkeypatch):
    client, repo = live
    monkeypatch.setattr(screener, "ScreenerService", lambda _: SimpleNamespace(
        latest_date=lambda: repo.day, _load_enriched_for_date=lambda _: repo.frame))
    payload = client.get("/api/screener/market-snapshot").json()
    assert len(payload["rows"]) == 2
    assert payload["rows"][0]["close"] == 6.25
    assert payload["rows"][1]["vol_ratio_5d"] is None
    assert repo.calls == []
