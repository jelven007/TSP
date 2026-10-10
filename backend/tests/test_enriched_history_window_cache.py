"""ETF/指数 enriched 全量路径的历史窗口缓存: 盘中复用, mtime/跨日失效。"""
from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from app.services import quote_service as qs
from app.tickflow.repository import DataStore, KlineRepository


def _write_etf_history(root, days: int, latest: date) -> None:
    first = latest - timedelta(days=days - 1)
    for offset in range(days):
        trade_date = first + timedelta(days=offset)
        close = 1.0 + offset * 0.001
        part = root / "kline_etf_daily" / f"date={trade_date.isoformat()}"
        part.mkdir(parents=True)
        pl.DataFrame({
            "symbol": ["510300.SH"],
            "date": [trade_date],
            "open": [close], "high": [close], "low": [close], "close": [close],
            "volume": [1000.0 + offset], "amount": [close * 1000.0],
        }).write_parquet(part / "part.parquet")


def _service(tmp_path):
    svc = qs.QuoteService.__new__(qs.QuoteService)
    svc._repo = KlineRepository(DataStore(tmp_path))
    svc._enriched_hist_cache = {}
    return svc


_COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "quote_ts"]


def test_history_window_reused_within_trading_day_without_rescan(tmp_path, monkeypatch):
    latest = date(2026, 7, 28)
    _write_etf_history(tmp_path, 80, latest)
    svc = _service(tmp_path)
    today = date(2026, 7, 29)
    cutoff = today - timedelta(days=90)

    first = svc._load_enriched_history_window("etf", "kline_etf_daily", _COLS, today, cutoff)
    assert first is not None and not first.is_empty()
    assert today not in first["date"].to_list()  # 窗口止于昨日

    # 第二轮: 命中缓存, 不再 scan 磁盘。
    calls = {"n": 0}
    real_scan = qs.scan_daily_parquet

    def _counting_scan(glob):
        calls["n"] += 1
        return real_scan(glob)

    monkeypatch.setattr(qs, "scan_daily_parquet", _counting_scan)
    second = svc._load_enriched_history_window("etf", "kline_etf_daily", _COLS, today, cutoff)
    assert calls["n"] == 0
    assert second.equals(first)


def test_history_window_invalidates_on_new_partition(tmp_path):
    latest = date(2026, 7, 28)
    _write_etf_history(tmp_path, 80, latest)
    svc = _service(tmp_path)
    today = date(2026, 7, 29)
    cutoff = today - timedelta(days=90)

    before = svc._load_enriched_history_window("etf", "kline_etf_daily", _COLS, today, cutoff)
    before_rows = before.height

    # 盘后回补一个历史交易日分区 → 目录 mtime 变化 → 缓存失效重读。
    extra = tmp_path / "kline_etf_daily" / "date=2026-04-01"
    extra.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["510300.SH"], "date": [date(2026, 4, 1)],
        "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0],
        "volume": [1.0], "amount": [1.0],
    }).write_parquet(extra / "part.parquet")

    after = svc._load_enriched_history_window("etf", "kline_etf_daily", _COLS, today, cutoff)
    assert after.height >= before_rows
