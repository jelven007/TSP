from datetime import date

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
