"""Closed-session quotes must retain their exact date and raw-price units."""
# ruff: noqa: F811 -- reuse the live endpoint fixture.
from datetime import date

import polars as pl
import pytest
from test_screener_live_snapshot import live  # noqa: F401

from app import market_time
from app.strategy import basic_filter


@pytest.mark.parametrize("closed", [date(2026, 11, 7), date(2026, 11, 8)])
def test_closed_session_accepts_exact_previous_session_cache(live, monkeypatch, closed):
    client, repo = live
    monkeypatch.setattr(market_time, "cn_today", lambda: closed)
    monkeypatch.setattr(market_time, "published_trading_day", lambda _: False)
    monkeypatch.setattr(basic_filter, "previous_session", lambda _: date(2026, 11, 6))
    payload = client.get("/api/screener/market-snapshot?realtime=true&symbols=600001.SH").json()
    assert payload["as_of"] == "2026-11-06"
    assert payload["requested_as_of"] == str(closed)
    assert payload["source"] == "previous_close"
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert [row[key] for key in ("raw_close", "change_pct", "turnover_rate", "vol_ratio_5d")] == [
        12.5, .02, 3.45, 1.8,
    ]
    assert repo.calls == [False]


def test_long_market_holiday_uses_published_predecessor_not_natural_yesterday(live, monkeypatch):
    client, repo = live
    closed, prior = date(2026, 10, 7), date(2026, 9, 30)
    repo.day = prior
    repo.frame = repo.frame.with_columns(pl.lit(prior).alias("date"))
    monkeypatch.setattr(market_time, "cn_today", lambda: closed)
    monkeypatch.setattr(market_time, "published_trading_day", lambda _: False)
    monkeypatch.setattr(basic_filter, "previous_session", lambda _: prior)
    payload = client.get("/api/screener/market-snapshot?realtime=true&symbols=600001.SH").json()
    assert payload["as_of"] == str(prior)
    assert payload["rows"][0]["raw_close"] == 12.5


def test_unknown_calendar_does_not_approve_old_cache(live, monkeypatch):
    client, _repo = live
    monkeypatch.setattr(market_time, "cn_today", lambda: date(2026, 11, 7))
    monkeypatch.setattr(market_time, "published_trading_day", lambda _: None)
    payload = client.get("/api/screener/market-snapshot?realtime=true&symbols=600001.SH").json()
    assert payload["as_of"] == "2026-11-07"
    assert payload["rows"] == []


def test_closed_cache_rejects_wrong_date_rows(live, monkeypatch):
    client, repo = live
    closed = date(2026, 11, 7)
    monkeypatch.setattr(market_time, "cn_today", lambda: closed)
    monkeypatch.setattr(market_time, "published_trading_day", lambda _: False)
    monkeypatch.setattr(basic_filter, "previous_session", lambda _: date(2026, 11, 6))
    repo.frame = repo.frame.with_columns(pl.lit(closed).alias("date"))
    payload = client.get("/api/screener/market-snapshot?realtime=true&symbols=600001.SH").json()
    assert payload["rows"] == []
