"""复牌昨收必须来自真实成交日, 不能受两日池或指标窗口截断。"""
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.services.screener import ScreenerService
from app.strategy.engine import StrategyDataContext
from app.strategy.late_session_strength import previous_closes
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 7, 7)


def test_previous_closes_merges_symbols_without_current_day_or_zero_trade_rows():
    pool = pl.DataFrame({
        "symbol": ["a", "b", "b"], "date": [DAY - timedelta(days=1)] * 2 + [DAY],
        "raw_close": [10.0, 0.0, 999.0], "volume": [1.0, 0.0, 1.0],
        "amount": [10.0, 0.0, 999.0],
    })
    daily = pl.DataFrame({
        "symbol": ["b", "b"], "date": [date(2026, 5, 5), DAY - timedelta(days=7)],
        "close": [123.0, 456.0], "raw_close": [12.0, 13.0],
        "volume": [1.0, 0.0], "amount": [12.0, 0.0],
    })
    context = StrategyDataContext("stock", "1m", DAY, pool_history=pool, daily_history=daily)
    assert previous_closes(context) == {"a": 10.0, "b": 12.0}
    assert previous_closes(context, exact_session=True) == {"a": 10.0}


def test_repository_last_traded_close_is_raw_strictly_prior_and_has_no_fixed_lookback(tmp_path):
    rows = [
        ("a", date(2026, 5, 5), 12.0, 1.0, 12.0),
        ("a", DAY - timedelta(days=1), 99.0, 0.0, 0.0),
        ("a", DAY, 999.0, 1.0, 999.0),
        ("a", DAY + timedelta(days=1), 9999.0, 1.0, 9999.0),
        ("b", DAY, 20.0, 1.0, 20.0),
        ("outside", DAY - timedelta(days=1), 30.0, 1.0, 30.0),
    ]
    frame = pl.DataFrame(rows, schema={"symbol": pl.String, "date": pl.Date, "close": pl.Float64,
                                      "volume": pl.Float64, "amount": pl.Float64}, orient="row")
    for (day,), partition in frame.partition_by("date", as_dict=True).items():
        folder = tmp_path / "kline_daily" / f"date={day}"
        folder.mkdir(parents=True)
        partition.write_parquet(folder / "part.parquet")
    store = DataStore(tmp_path)
    try:
        repo = KlineRepository(store)
        assert repo.get_last_traded_closes(["a", "b"], DAY).to_dicts() == [
            {"symbol": "a", "date": date(2026, 5, 5), "raw_close": 12.0}]
        assert repo.get_last_traded_closes([], DAY).is_empty()
    finally:
        store.db.close()


def test_screener_loads_missing_long_halt_reference_without_changing_pool_dates():
    current = pl.DataFrame({"symbol": ["a", "b"], "date": [DAY] * 2, "raw_close": [11.0, 14.0]})
    prior = pl.DataFrame({"symbol": ["a"], "date": [DAY - timedelta(days=1)], "raw_close": [10.0]})
    old = pl.DataFrame({"symbol": ["b"], "date": [date(2026, 5, 5)], "raw_close": [12.0]})
    calls = []

    class Repo:
        def get_enriched_history(self, *args):
            return None

        def get_instruments_asset(self, *args):
            return pl.DataFrame({"symbol": ["a", "b", "halted"], "listing_date": [date(2000, 1, 1)] * 3})

        def get_last_traded_closes(self, symbols, before):
            calls.append((symbols, before))
            return old

    service = ScreenerService(Repo())
    service._load_minute_history = lambda *args: pl.DataFrame({"symbol": ["a", "b"]})
    service.build_daily_pool_context = lambda *args, **kwargs: StrategyDataContext(
        "stock", "1d", DAY, current=current, pool_history=prior)
    engine = SimpleNamespace(minute_daily_history_bars=lambda *args, **kwargs: 0)
    context = service.build_strategy_context(engine, DAY, [], timeframe="1m", current=current)
    assert previous_closes(context) == {"a": 10.0, "b": 12.0}
    assert calls == [(["b"], DAY)]
    assert context.pool_history.filter(pl.col("date") == DAY - timedelta(days=1)).height == 1
    assert context.daily_history is None


def test_last_traded_close_does_not_open_unneeded_older_partition(tmp_path):
    recent = tmp_path / "kline_daily" / f"date={DAY - timedelta(days=1)}"
    recent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["a"], "date": [DAY - timedelta(days=1)], "close": [12.0],
                  "volume": [1.0], "amount": [12.0]}).write_parquet(recent / "part.parquet")
    store = DataStore(tmp_path)
    try:
        # An unrelated archive must not prevent reading a complete recent reference.
        older = tmp_path / "kline_daily" / "date=2023-05-30"
        older.mkdir()
        (older / "part.parquet").write_bytes(b"unavailable unrelated archive")
        assert KlineRepository(store).get_last_traded_closes(["a"], DAY).to_dicts() == [
            {"symbol": "a", "date": DAY - timedelta(days=1), "raw_close": 12.0}]
    finally:
        store.db.close()


def test_signal_replay_uses_last_real_close_beyond_fifteen_calendar_days(tmp_path):
    from datetime import datetime

    from app.services.daily_stock_pools import DailyStockPools
    from app.services.late_session_replay import replay
    from app.services.strategy_results import ResultIdentity

    identity = ResultIdentity("s", "示例", "v1", "{}", "{}", "null", "stock", "1m")
    DailyStockPools(tmp_path).save(identity, {
        "strategy_id": "s", "as_of": str(DAY), "symbols": ["a"], "total": 1,
    }, started_at=1)
    target = datetime.combine(DAY, datetime.strptime("14:30:00", "%H:%M:%S").time())
    prices = [10.1 + i * 0.001 for i in range(60)]
    minute = pl.DataFrame({
        "symbol": ["a"] * 60,
        "datetime": [target - timedelta(minutes=59 - i) for i in range(60)],
        **{column: prices for column in ("open", "high", "low", "close")},
        "volume": [100.0] * 60, "amount": [1000.0] * 60,
    })
    calls = []

    class Repo:
        def get_daily_batch(self, *args, **kwargs):
            raise AssertionError("A fixed fifteen-day window loses the resumed stock")

        def get_last_traded_closes(self, symbols, before):
            calls.append((symbols, before))
            return pl.DataFrame({"symbol": ["a"], "date": [date(2026, 5, 5)], "raw_close": [10.0]})

        def get_minute_batch(self, *args, **kwargs):
            return minute

    result = replay(Repo(), tmp_path, {"id": "late_session_strength", "params": {}},
                    strategy_id="s", start=DAY, end=DAY, symbols=["a"])
    assert calls == [(["a"], DAY)]
    assert result["diagnostics"] == []
    assert result["triggers"] == [{"date": str(DAY), "time": "14:30:00", "symbol": "a"}]
