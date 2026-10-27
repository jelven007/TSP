"""盘后管道的「今天」必须是北京日期, 不能用服务器本地 date.today()。

调度默认 16:00 北京时间。美西主机此时本地日历日仍是昨天: 未修复代码
today_exists 把「北京昨天的日K」当成今天已齐, 窗口右端停在昨天, 当日官方
收盘价永远拉不进来。UTC 主机在北京 00:00-08:00 有同样错位。
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import instrument_sync, kline_sync

BJ = date(2026, 3, 2)
LOCAL = date(2026, 3, 1)


class _StopError(Exception):
    """日K窗口已经记下, 后面的管道阶段与本 bug 无关。"""


def test_pipeline_daily_window_ends_on_beijing_today(monkeypatch, tmp_path) -> None:
    """数据修正/补拉路径的窗口右端必须是北京今天。

    raising=False: 未修复代码没有调用 cn_today, 钉了也不会被用到,
    仍走 date.today() → 与 2026-03-02 不等。
    """
    captured: list[date] = []

    def fake_batch(universe, repo, capset, start_date=None, end_date=None, on_chunk_done=None):
        captured.append(end_date.date() if hasattr(end_date, "date") else end_date)
        raise _StopError()

    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: BJ, raising=False)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir, **kwargs: 0)
    monkeypatch.setattr(
        instrument_sync,
        "load_current_stock_pool",
        lambda data_dir, **kwargs: SimpleNamespace(
            symbols=("600000.SH",),
            as_of=LOCAL,
            source="test",
            boards=("沪主板",),
        ),
    )
    monkeypatch.setattr(daily_pipeline, "_resolve_universe", lambda capset, repo=None: ["600000.SH"])
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", fake_batch)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: LOCAL,
    )
    capset = SimpleNamespace(has=lambda key: False)

    with pytest.raises(_StopError):
        daily_pipeline.run_now(repo, capset, override_start_date=LOCAL)  # type: ignore[arg-type]

    assert captured, "应发起日K范围拉取"
    assert captured[0] == BJ, f"窗口右端必须是北京日期 {BJ}, 实际 {captured[0]} (服务器本地 {date.today()})"


class _RangeObserved(BaseException):
    """Stop after the relevant real pipeline stages without settlement or publication."""


def test_explicit_daily_repair_bounds_stock_index_etf_factors_and_cleanup(monkeypatch, tmp_path):
    start, end, later = date(2026, 2, 26), date(2026, 2, 27), date(2026, 3, 2)
    before = date(2026, 2, 25)
    captured = {}
    for day in (before, start, end, later):
        for table in ("kline_daily", "kline_daily_enriched", "kline_etf_daily"):
            part = tmp_path / table / f"date={day}"
            part.mkdir(parents=True)
            pl.DataFrame({
                "symbol": ["600000.SH"], "date": [day],
                "open": [10.], "high": [11.], "close": [10.], "raw_close": [9.],
                "volume": [100.], "amount": [1000.], "quote_time_basis": ["received"],
                "quote_ts": [int(datetime.combine(day, datetime.min.time()).timestamp() * 1000)],
            }).write_parquet(part / "part.parquet")
    preserved = {
        day: (tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet").read_bytes()
        for day in (before, later)
    }
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: BJ)
    monkeypatch.setattr(instrument_sync, "load_current_stock_pool", lambda *a, **kw: SimpleNamespace(
        symbols=("600000.SH",), as_of=LOCAL, source="test", boards=("沪主板",),
    ))
    for name in ("_invalidate", "_refresh_single_view"):
        monkeypatch.setattr(daily_pipeline, name, lambda *a: None)
    monkeypatch.setattr(daily_pipeline, "warn_if_enriched_too_thin", lambda *a: 4)
    monkeypatch.setattr(daily_pipeline._prefs, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_index", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_pull_etf", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_minute_sync_enabled", lambda: True)
    monkeypatch.setattr(daily_pipeline._prefs, "get_pipeline_regime_enabled", lambda: False)

    def batch(key):
        def sync(*a, **kw):
            captured[key] = kw
            return 1
        return sync

    monkeypatch.setattr(kline_sync, "sync_and_persist_daily_batch", batch("stock"))
    monkeypatch.setattr(kline_sync, "sync_adj_factor", lambda *a, **kw: (captured.update(stock_adj=kw) or 0, []))
    monkeypatch.setattr(kline_sync, "sync_and_persist_minute", lambda *a, **kw: pytest.fail("daily range must not acquire minutes"))
    monkeypatch.setattr(daily_pipeline, "run_pipeline", lambda **kw: 1)
    from app.services import index_sync
    monkeypatch.setattr(index_sync, "sync_index_instruments", lambda *a, **kw: 1)
    monkeypatch.setattr(index_sync, "sync_etf_instruments", lambda *a, **kw: 1)
    monkeypatch.setattr(index_sync, "sync_and_persist_index_daily", batch("index"))
    monkeypatch.setattr(index_sync, "sync_and_persist_etf_daily", batch("etf"))
    monkeypatch.setattr(index_sync, "sync_etf_adj_factor", lambda *a, **kw: (captured.update(etf_adj=kw) or 0, []))
    def stop(*a):
        raise _RangeObserved()
    monkeypatch.setattr("app.services.corporate_actions.sync_actions", stop)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), latest_daily_date=lambda: later,
        symbols_lagging=lambda *a, **kw: [], refresh_index_views=lambda: None,
        get_etf_instruments=lambda: pl.DataFrame({"symbol": ["510300.SH"]}),
    )
    with pytest.raises(_RangeObserved):
        daily_pipeline.run_now(repo, SimpleNamespace(has=lambda _: True),
                               override_start_date=start, override_end_date=end)
    for key in ("stock", "index", "etf"):
        assert captured[key]["start_date"].date() == start
        assert captured[key]["end_date"].date() == end
    for key in ("stock_adj", "etf_adj"):
        assert captured[key]["start_time"].date() == start
        assert captured[key]["end_time"] == datetime.combine(end, datetime.max.time())
    for day, content in preserved.items():
        assert (tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet").read_bytes() == content
    for day in (start, end):
        assert not (tmp_path / "kline_daily_enriched" / f"date={day}").exists()
    assert (tmp_path / "kline_etf_daily" / f"date={later}" / "part.parquet").exists()


@pytest.mark.parametrize(("start", "end"), [(None, LOCAL), (BJ, LOCAL), (LOCAL, date(2026, 3, 3))])
def test_pipeline_rejects_invalid_explicit_range_before_reading_pool(monkeypatch, start, end):
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: BJ)
    monkeypatch.setattr(instrument_sync, "load_current_stock_pool", lambda *a, **kw: pytest.fail("invalid range must stop first"))
    with pytest.raises(ValueError):
        daily_pipeline.run_now(None, None, override_start_date=start, override_end_date=end)
