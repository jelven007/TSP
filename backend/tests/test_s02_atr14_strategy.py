"""Full S02 source ranks causally without shrinking trigger membership."""
import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

SOURCE = Path(__file__).resolve().parents[2] / "examples/strategies/s02_atr14.py"
DAY = date(2026, 11, 2)


def prior(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


@pytest.fixture
def strategy(monkeypatch):
    spec = importlib.util.spec_from_file_location("s02_atr14_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "previous_session", prior, raising=False)
    return module


def frames(symbols=("600001.SH", "600002.SH"), ranges=(1., 2.), changes=(.04, .01)):
    days = []
    current = DAY
    for _ in range(15):
        current = prior(current)
        days.append(current)
    days.reverse()
    daily = pl.DataFrame([
        {"symbol": symbol, "date": day, "high": 10. + width / 2,
         "low": 10. - width / 2, "close": 10., "raw_close": 20.}
        for symbol, width in zip(symbols, ranges, strict=True) for day in days
    ])
    minute = pl.DataFrame([
        {"symbol": symbol, "datetime": datetime(2026, 11, 2, 14, 30),
         "close": 20. * (1 + change), "csgi_late_session_strength": True}
        for symbol, change in zip(symbols, changes, strict=True)
    ])
    return minute, daily


def saved_selection(strategy, minute, daily):
    rows = minute.select("symbol").unique(maintain_order=True).with_columns(
        pl.lit(20.).alias("reference_close"),
    )
    enriched = strategy.enrich_selection(rows, {}, daily=daily, as_of=DAY)
    return {"source_date": str(prior(DAY)), "rows": enriched.to_dicts()}


def test_atr_beats_opposite_trigger_change_and_uses_adjusted_denominator(strategy):
    minute, daily = frames()
    selection = saved_selection(strategy, minute, daily)
    rows = strategy.filter_minute_history(minute, {}, selection=selection).to_dicts()
    assert [row["symbol"] for row in rows] == ["600002.SH", "600001.SH"]
    assert [row["atr14_previous_close"] for row in rows] == pytest.approx([.2, .1])
    assert [row["change_pct"] for row in rows] == pytest.approx([.01, .04])
    assert rows[0]["score"] > rows[1]["score"]
    assert all(row["atr_reference_date"] == prior(DAY) for row in rows)


def test_simple_atr_not_wilder_or_today_close(strategy):
    minute, daily = frames(("600001.SH",), (1.,), (.02,))
    daily = daily.with_columns(
        pl.when(pl.col("date") == prior(DAY)).then(12.).otherwise(pl.col("high")).alias("high"),
        pl.when(pl.col("date") == prior(DAY)).then(8.).otherwise(pl.col("low")).alias("low"),
    )
    selection = saved_selection(strategy, minute, daily)
    row = strategy.filter_minute_history(minute, {}, selection=selection).row(0, named=True)
    assert row["atr14"] == pytest.approx(17 / 14)
    assert row["atr14_previous_close"] == pytest.approx(17 / 140)


def test_future_daily_values_cannot_change_rank(strategy):
    minute, daily = frames()
    future = daily.filter(pl.col("date") == prior(DAY)).with_columns(
        pl.lit(DAY).alias("date"), pl.lit(1e6).alias("high"),
    )
    selection = saved_selection(strategy, minute, daily)
    expected = strategy.filter_minute_history(minute, {}, selection=selection)
    actual = strategy.filter_minute_history(
        minute, {}, daily=pl.concat([daily, future]), selection=selection,
    )
    assert actual.equals(expected)


def test_all_hits_retained_first_trigger_keeps_saved_ties_by_symbol(strategy):
    symbols = tuple(f"60000{i}.SH" for i in range(1, 7))
    minute, daily = frames(symbols, (2.,) * 6, (.02, .03, .03, .01, .01, .01))
    later = minute.with_columns(pl.lit(datetime(2026, 11, 2, 15)).alias("datetime"),
                                pl.lit(99.).alias("close"))
    selection = saved_selection(strategy, minute, daily)
    result = strategy.filter_minute_history(pl.concat([later, minute]), {}, selection=selection)
    assert result.height == 6
    assert result["symbol"].to_list() == list(symbols)
    assert set(result["last_datetime"]) == {datetime(2026, 11, 2, 14, 30)}


@pytest.mark.parametrize("damage", ["missing", "duplicate", "bad_ohlc", "bad_raw", "nan"])
def test_invalid_atr_window_preserves_selection_and_signal_with_unknown_rank(strategy, damage):
    minute, daily = frames()
    first = daily.row(0, named=True)
    if damage == "missing":
        daily = daily.slice(1)
    elif damage == "duplicate":
        daily = pl.concat([daily, daily.head(1)])
    else:
        field = {"bad_ohlc": "low", "bad_raw": "raw_close", "nan": "close"}[damage]
        value = {"bad_ohlc": 30., "bad_raw": 0., "nan": float("nan")}[damage]
        daily = daily.with_columns(
            pl.when((pl.col("symbol") == first["symbol"]) & (pl.col("date") == first["date"]))
            .then(value).otherwise(pl.col(field)).alias(field))
    selection = saved_selection(strategy, minute, daily)
    damaged = next(row for row in selection["rows"] if row["symbol"] == first["symbol"])
    assert len(selection["rows"]) == 2
    assert damaged["atr14_previous_close"] is None
    assert damaged["atr_rank"] is None
    assert "ATR14" in damaged["atr_metric_error"]
    result = strategy.filter_minute_history(minute, {}, selection=selection)
    assert result.height == 2
    assert result["symbol"].to_list() == ["600002.SH", "600001.SH"]
    assert result["atr_rank"].to_list() == [1, None]
    assert result["score"].to_list() == [50., None]
    assert result["atr_metric_error"][-1] == damaged["atr_metric_error"]


def test_missing_atr_history_preserves_hits_but_unknown_calendar_blocks_preselection(strategy, monkeypatch):
    minute, daily = frames()
    selection = saved_selection(strategy, minute, None)
    assert all(row["atr14_previous_close"] is None for row in selection["rows"])
    assert all("ATR14" in row["atr_metric_error"] for row in selection["rows"])
    result = strategy.filter_minute_history(minute, {}, selection=selection)
    assert result.height == 2
    assert result["score"].null_count() == 2
    monkeypatch.setattr(strategy, "previous_session",
                        lambda _: (_ for _ in ()).throw(ValueError("unknown calendar")))
    with pytest.raises(ValueError, match="unknown calendar"):
        saved_selection(strategy, minute, daily)
    assert strategy.filter_minute_history(minute, {}, selection=selection).equals(result)


def test_no_trigger_needs_no_atr_history(strategy):
    minute, _ = frames()
    empty = minute.with_columns(pl.lit(False).alias("csgi_late_session_strength"))
    assert strategy.filter_minute_history(empty, {}).is_empty()


def test_engine_load_and_empty_weights_preserve_explicit_score():
    from app.strategy.engine import StrategyEngine
    engine = StrategyEngine([SOURCE.parent])
    assert not engine.load_errors()
    definition = engine.get("custom_mvxxcp88")
    assert definition.meta["scoring"] == {}
    frame = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "score": [2., 1.]})
    assert engine._apply_scoring(frame, {}).equals(frame)


@pytest.mark.parametrize("weights", [{}, {"change_pct": 1}, {"atr14_previous_close": 1}])
def test_engine_atr_ranking_cannot_be_replaced_by_saved_weights(strategy, monkeypatch, weights):
    from app.api.strategy import _strategy_detail
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    engine = StrategyEngine([SOURCE.parent])
    definition = engine.get("custom_mvxxcp88")
    definition.filter_minute_history_fn = strategy.filter_minute_history
    monkeypatch.setattr(engine, "_inject_intraday_signal_columns", lambda frame, **kw: frame)
    minute, daily = frames()
    context = StrategyDataContext(
        "stock", "1m", DAY, history=minute, daily_history=daily,
        selection_snapshots={definition.meta["id"]: saved_selection(strategy, minute, daily)},
    )
    overrides = {"scoring": weights, "scoring_replace": True,
                 "scoring_directions": {"atr14_previous_close": "low"},
                 "entry_signals": [], "exit_signals": []}
    result = engine._run_strategy(definition.meta["id"], context, overrides=overrides)
    assert [row["symbol"] for row in result.rows] == ["600002.SH", "600001.SH"]
    assert [row["score"] for row in result.rows] == pytest.approx([100., 0.])
    assert [row["atr_rank"] for row in result.rows] == [1, 2]
    assert _strategy_detail(definition, overrides)["scoring"] == {}
    assert _strategy_detail(definition, overrides)["scoring_mode"] == "strategy"


def test_safe_calendar_entry_uses_published_weekend_and_rejects_unknown(monkeypatch):
    from app.strategy import basic_filter, market_data
    basic_filter.previous_session.cache_clear()
    monkeypatch.setattr(basic_filter, "published_trading_day",
                        lambda day: day.weekday() < 5 if day >= date(2026, 10, 30) else None)
    assert market_data.previous_session(DAY) == date(2026, 10, 30)
    with pytest.raises(ValueError, match="缺少已发布交易日历"):
        market_data.previous_session(date(2026, 10, 30))
    basic_filter.previous_session.cache_clear()
