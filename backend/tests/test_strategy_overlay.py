from datetime import date, datetime

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_market_data_matrix,
    make_signal_matrix,
)
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.overlay import frame_mask, normalize_conditions, warmup_bars


def _panel():
    return pl.DataFrame({
        "symbol": ["A", "B", "A", "B", "B", "A", "B"],
        "date": [date(2024, 1, d) for d in [2, 2, 3, 3, 4, 5, 5]],
        "close": [10., 20., 12., 19., 18., 11., 21.],
        "open": [10., 20., 12., 19., 18., 11., 21.],
        "high": [10., 20., 12., 19., 18., 11., 21.],
        "low": [10., 20., 12., 19., 18., 11., 21.],
        "volume": [100.] * 7,
    })


def test_offset_uses_symbol_trading_rows_and_preserves_input_order():
    panel = _panel().reverse()
    conditions = [{"left": "close", "op": ">", "right": "field:close", "rightDays": 1}]
    mask = frame_mask(panel, conditions)
    assert panel.filter(mask).sort(["date", "symbol"])["symbol"].to_list() == ["A", "B"]


@pytest.mark.parametrize("patch", [
    {"leftDays": -1}, {"rightDays": 61}, {"leftDays": 1.5}, {"leftDays": True},
    {"right": "nan"}, {"right": float("inf")}, {"right": True},
    {"left": "symbol"}, {"op": "eval"}, {"right": "field:missing"},
])
def test_invalid_condition_fails_closed(patch):
    with pytest.raises(ValueError):
        normalize_conditions([{"left": "close", "op": ">", "right": "0", **patch}])


def test_limits_nulls_and_missing_dependencies():
    assert normalize_conditions([]) == []
    with pytest.raises(ValueError):
        normalize_conditions([{"left": "close", "op": ">", "right": "0"}] * 9)
    with pytest.raises(ValueError, match="ma20"):
        frame_mask(_panel(), [{"left": "close", "op": ">", "right": "field:ma20"}])
    panel = _panel().with_columns(pl.lit(float("nan")).alias("close"))
    assert not frame_mask(panel, [{"left": "close", "op": "!=", "right": "0"}]).any()
    assert warmup_bars([{"left": "ma60", "op": ">", "right": "0", "leftDays": 60}]) >= 120


class _Signals:
    def compute_signals(self, market, params):
        entry = np.ones(market.shape, dtype=np.uint8)
        exit_ = np.zeros(market.shape, dtype=np.uint8)
        exit_[-1, 0] = 1
        return make_signal_matrix(market.shape, entry=entry, exit=exit_)


def test_matrix_matches_frame_on_sparse_calendar_and_keeps_exits():
    conditions = [{"left": "close", "op": ">", "right": "field:close", "rightDays": 1}]
    panel = _panel()
    market = build_market_data_matrix(panel)
    result = MatrixStrategyPipeline().run(_Signals(), market, {}, MatrixPipelineConfig(
        basic_filter={}, scoring={}, order_by=None, descending=True, overlay_filter=conditions,
    ))
    expected = panel.filter(frame_mask(panel, conditions))
    assert int(result.entry.sum()) == expected.height
    assert result.exit[-1, 0] == 1
    assert result.entry[-1, 0] == 0


def test_offset_does_not_skip_a_null_field_on_an_existing_bar():
    from app.backtest.matrix import build_overlay_mask

    conditions = [{"left": "amount", "op": ">", "right": "0", "leftDays": 1}]
    panel = _panel().with_columns(
        pl.when(pl.col("date") == date(2024, 1, 3)).then(None).otherwise(100.).alias("amount"),
    )
    market = build_market_data_matrix(panel, field_columns={"amount"})
    allowed = build_overlay_mask(market, conditions)
    for row in panel.with_columns(frame_mask(panel, conditions).alias("_allowed")).iter_rows(named=True):
        assert bool(allowed[market.timestamp_labels.index(str(row["date"])), market.symbols.index(row["symbol"])]) == row["_allowed"]


def test_config_roundtrip_and_invalid_write_preserves_previous_config(tmp_path):
    from fastapi import HTTPException
    from test_strategy_detail_signals import _make_strategy

    from app.api.strategy import _strategy_detail, _validate_overlay_config
    from app.strategy.config import load_override, save_override

    strategy = _make_strategy([], [])
    overrides = {"overlay_filter": [{"left": "close", "op": ">", "right": "field:ma20"}]}
    _validate_overlay_config(overrides, strategy)
    save_override(tmp_path, "s", overrides)
    assert _strategy_detail(strategy, load_override(tmp_path, "s"))["overlay_filter"] == overrides["overlay_filter"]
    with pytest.raises(HTTPException):
        _validate_overlay_config({"overlay_filter": [{"left": "close", "op": ">", "right": "inf"}]}, strategy)
    assert load_override(tmp_path, "s") == overrides
    save_override(tmp_path, "s", {"overlay_filter": []})
    assert load_override(tmp_path, "s")["overlay_filter"] == []


def test_snapshot_filter_uses_full_history_before_base_filter_and_keeps_exit(tmp_path):
    (tmp_path / "overlay.py").write_text(
        'import polars as pl\n'
        'META = {"id": "s", "name": "S", "scoring": {}, "limit": 30}\n'
        'BASIC_FILTER = {"enabled": False}\n'
        'EXIT_SIGNALS = ["sell"]\n'
        'def filter(df, params): return pl.lit(True)\n',
    )
    engine = StrategyEngine([tmp_path])
    panel = _panel().with_columns((pl.col("close") == 11).alias("signal_sell"))
    conditions = [{"left": "close", "op": ">", "right": "field:close", "rightDays": 1}]
    result = engine.run("s", StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2024, 1, 5),
        current=panel.filter(pl.col("date") == date(2024, 1, 5)), history=panel,
    ), overrides={"overlay_filter": conditions})
    assert [r["symbol"] for r in result.rows] == ["B"]
    assert [r["symbol"] for r in result.exit_signal_hits] == ["A"]
    assert engine.required_history_bars(["s"], overrides_map={"s": {"overlay_filter": conditions}}) >= 2


def test_missing_overlay_data_blocks_entries_and_keeps_explicit_exit(tmp_path):
    (tmp_path / "overlay.py").write_text(
        'import polars as pl\n'
        'META = {"id": "s", "name": "S", "scoring": {}}\n'
        'BASIC_FILTER = {"enabled": False}\n'
        'EXIT_SIGNALS = ["sell"]\n'
        'def filter(df, params): return pl.lit(True)\n',
    )
    engine = StrategyEngine([tmp_path])
    current = _panel().filter(pl.col("date") == date(2024, 1, 5)).with_columns(
        (pl.col("symbol") == "A").alias("signal_sell"),
    )
    result = engine.run("s", StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2024, 1, 5), current=current,
    ), overrides={"overlay_filter": [{"left": "amount", "op": ">", "right": "0"}]})
    assert result.rows == [] and result.entry_signal_hits == []
    assert [hit["symbol"] for hit in result.exit_signal_hits] == ["A"]
    assert "amount" in result.entry_filter_error


def test_missing_matrix_overlay_field_still_delivers_exit():
    from dataclasses import replace

    from test_strategy_detail_signals import _make_strategy

    strategy = replace(
        _make_strategy([], []), execution_backend="matrix_native", matrix_strategy=_Signals(),
        basic_filter={}, meta={"id": "s", "scoring": {}},
    )
    engine = StrategyEngine([])
    engine._strategies["s"] = strategy
    panel = _panel()
    result = engine.run("s", StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2024, 1, 5),
        history=panel, current=panel.filter(pl.col("date") == date(2024, 1, 5)),
        market=build_market_data_matrix(panel),
    ), overrides={"overlay_filter": [{"left": "amount", "op": ">", "right": "0"}]})
    assert result.rows == [] and result.entry_signal_hits == []
    assert [hit["symbol"] for hit in result.exit_signal_hits] == ["A"]
    assert "amount" in result.entry_filter_error


@pytest.fixture
def minute_overlay_engine(tmp_path):
    (tmp_path / "minute.py").write_text(
        'META = {"id": "minute", "name": "Minute", "scoring": {},'
        '"execution_backend": "minute_filter", "timeframes": ["1m"]}\n'
        'BASIC_FILTER = {"enabled": False}\n'
        'EXIT_SIGNALS = ["sell"]\n'
        'def filter_minute_history(df, params):\n'
        ' return df.rename({"datetime": "last_datetime"})\n',
    )
    return StrategyEngine([tmp_path])


def minute_overlay_context(*, bars=20, missing_amount=False):
    from app.strategy.basic_filter import previous_session

    day = date(2026, 9, 4)
    dates, before = [], day
    for _ in range(bars):
        before = previous_session(before)
        dates.append(before)
    daily = pl.DataFrame([
        {"symbol": symbol, "date": at, "close": 10., "amount": amount}
        for symbol, amount in [("600000.SH", 1e8), ("600036.SH", .9999e8)] for at in dates
    ])
    if missing_amount:
        daily = daily.drop("amount")
    current = pl.DataFrame({
        "symbol": ["600000.SH", "600036.SH"], "date": [day, day], "close": [10., 10.],
        "amount": [1e15, 1e15], "signal_sell": [False, False],
    })
    return StrategyDataContext(
        "stock", "1m", day, current=current, daily_history=daily,
        history=pl.DataFrame({"symbol": ["600000.SH", "600036.SH"], "datetime": [datetime(2026, 9, 4, 14, 30)]*2,
                              "close": [10., 10.], "amount": [1e4, 1e4]}),
    )


AMOUNT_OVERLAY = [{"left": "amount_mean_20d", "op": ">=", "right": "1.0",
                   "leftDays": 0, "rightDays": 0}]


def test_minute_overlay_plans_completed_daily_warmup(minute_overlay_engine):
    required = minute_overlay_engine.minute_daily_history_bars(
        ["minute"], overrides_map={"minute": {"overlay_filter": AMOUNT_OVERLAY}},
    )
    assert required >= 21


def test_generic_minute_overlay_config_is_accepted(minute_overlay_engine):
    from app.api.strategy import _validate_overlay_config

    overrides = {"overlay_filter": AMOUNT_OVERLAY}
    _validate_overlay_config(overrides, minute_overlay_engine.get("minute"))
    assert overrides["overlay_filter"] == AMOUNT_OVERLAY


@pytest.mark.parametrize("future_daily", [False, True])
def test_minute_amount_overlay_uses_twenty_completed_days_and_preserves_pool(
    minute_overlay_engine, future_daily,
):
    context = minute_overlay_context()
    if future_daily:
        context.daily_history = pl.concat([
            context.daily_history, context.current.select("symbol", "date", "close", "amount"),
        ])
    result = minute_overlay_engine.run("minute", context, overrides={"overlay_filter": AMOUNT_OVERLAY})
    assert [row["symbol"] for row in result.rows] == ["600000.SH"]
    assert result.daily_stock_pool["total"] == 2


def test_minute_amount_overlay_does_not_average_nineteen_days(minute_overlay_engine):
    result = minute_overlay_engine.run(
        "minute", minute_overlay_context(bars=19), overrides={"overlay_filter": AMOUNT_OVERLAY},
    )
    assert not result.rows and not result.entry_signal_hits


def test_minute_missing_daily_overlay_keeps_explicit_exit(minute_overlay_engine):
    context = minute_overlay_context(missing_amount=True)
    context.current = context.current.with_columns((pl.col("symbol") == "600000.SH").alias("signal_sell"))
    result = minute_overlay_engine.run("minute", context, overrides={"overlay_filter": AMOUNT_OVERLAY})
    assert not result.rows and "amount_mean_20d" in result.entry_filter_error
    assert [row["symbol"] for row in result.exit_signal_hits] == ["600000.SH"]


def test_specialized_minute_execution_still_rejects_daily_overlay(minute_overlay_engine):
    from dataclasses import replace

    from fastapi import HTTPException

    from app.api.strategy import _validate_overlay_config

    strategy = minute_overlay_engine.get("minute")
    minute_overlay_engine._strategies["minute"] = replace(
        strategy, meta={**strategy.meta, "minute_execution": "unsupported_specialized"},
    )
    with pytest.raises(ValueError, match="专用分钟"):
        minute_overlay_engine.run("minute", minute_overlay_context(),
                                  overrides={"overlay_filter": AMOUNT_OVERLAY})
    with pytest.raises(HTTPException, match="专用分钟"):
        _validate_overlay_config({"overlay_filter": AMOUNT_OVERLAY}, minute_overlay_engine.get("minute"))


def test_minute_change_scoring_uses_signal_close_and_exact_raw_previous_close(minute_overlay_engine):
    context = minute_overlay_context()
    context.history = context.history.with_columns(pl.Series("close", [10.1, 10.2]))
    context.current = context.current.with_columns(
        pl.Series("close", [50., 1.]), pl.Series("change_pct", [.9, -.9]),
    )
    context.daily_history = context.daily_history.with_columns(
        pl.lit(99.).alias("close"), pl.lit(10.).alias("raw_close"),
    )
    overrides = {"scoring": {"change_pct": 1}, "scoring_replace": True,
                 "scoring_directions": {"change_pct": "high"}}
    assert minute_overlay_engine.minute_daily_history_bars(["minute"], overrides_map={"minute": overrides}) >= 2
    result = minute_overlay_engine.run("minute", context, overrides=overrides)
    assert [row["symbol"] for row in result.rows] == ["600036.SH", "600000.SH"]
    assert [row["change_pct"] for row in result.rows] == pytest.approx([.02, .01])


def test_minute_scoring_cannot_substitute_stale_previous_close(minute_overlay_engine):
    from app.strategy.basic_filter import previous_session

    context = minute_overlay_context()
    context.daily_history = context.daily_history.filter(
        pl.col("date") != previous_session(context.as_of),
    )
    result = minute_overlay_engine.run("minute", context, overrides={"scoring": {"change_pct": 1}})
    assert not result.rows
