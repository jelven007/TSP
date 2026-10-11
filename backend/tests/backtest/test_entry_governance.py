from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_basic_filter_mask,
    build_market_data_matrix,
    build_market_matrix,
    load_market_data_matrix_from_parquet,
    make_signal_matrix,
)
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine


def panel():
    return pl.DataFrame({
        "symbol": ["600001.SH"] * 5,
        "name": ["正常公司"] * 5,
        "date": [date(2024, 2, 1) + timedelta(days=i) for i in range(5)],
        **{c: [10.0] * 5 for c in ("open", "high", "low", "close")},
        "volume": [100_000.0] * 5,
        "is_risk_warning": [False, True, False, True, True],
        "instrument_status_known": [True] * 5,
        "is_listed": [True] * 5,
        "listing_age_days": [29.0, 30.0, None, -1.0, 31.0],
    })


@pytest.mark.parametrize("independent", [False, True])
@pytest.mark.parametrize("exclude_st", [False, True])
def test_execution_rechecks_st_without_blocking_exits(independent, exclude_st):
    df = panel()
    matrix = build_market_matrix(
        df, pl.Series([True, True, False, False, False]),
        pl.Series([False, False, True, False, False]),
        entry_delay_bars=1, exit_delay_bars=1,
        basic_filter={"exclude_st": exclude_st},
    )
    engine = BacktestEngine(None)
    config = MatcherConfig(matching="open_t+1", max_positions=1)
    result = (
        engine.simulate_independent_market_matrix(matrix, 2, config)
        if independent else engine.simulate_market_matrix(matrix, config)
    )
    assert result.trades
    assert all(t.exit_date == "2024-02-04" for t in result.trades)
    if exclude_st:
        assert {t.entry_date for t in result.trades} == {"2024-02-03"}
        assert result.stats["execution"]["buy_ineligible"] == 1
    else:
        assert "2024-02-02" in {t.entry_date for t in result.trades}


def test_new_stock_age_filter_matches_polars_and_honors_boundary_and_disable():
    df = panel()
    market = build_market_data_matrix(df, field_columns={"listing_age_days"})
    bf = {"exclude_new_days": 30}
    assert build_basic_filter_mask(market, bf)[:, 0].tolist() == [False, True, False, False, True]
    expr = StrategyEngine._basic_filter_expr(df, bf)
    assert df.filter(expr)["date"].to_list() == [date(2024, 2, 2), date(2024, 2, 5)]
    assert build_basic_filter_mask(market, {"exclude_new_days": 0}).all()
    assert build_basic_filter_mask(market, {**bf, "enabled": False}).all()
    missing = build_market_data_matrix(df.drop("listing_age_days"))
    assert not build_basic_filter_mask(missing, bf).any()


@pytest.mark.parametrize("entry_delay,exit_delay", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_conflicting_signal_is_removed_before_execution_delay(entry_delay, exit_delay):
    matrix = build_market_matrix(
        panel(), pl.Series([True, False, False, False, False]),
        pl.Series([True, False, False, False, False]),
        entry_delay_bars=entry_delay, exit_delay_bars=exit_delay,
    )
    assert not matrix.entry.any()
    assert matrix.exit.sum() == 1


def test_pipeline_exit_priority_applies_before_scoring():
    market = build_market_data_matrix(panel())

    class Both:
        def compute_signals(self, market, params):
            return make_signal_matrix(
                market.shape, entry=np.ones(market.shape, dtype=np.uint8),
                exit=np.ones(market.shape, dtype=np.uint8),
            )

    signals = MatrixStrategyPipeline().run(
        Both(), market, {}, MatrixPipelineConfig(
            basic_filter={}, scoring={"close": 1}, order_by="score", descending=True,
        ),
    )
    assert not signals.entry.any()
    assert not signals.score.any()
    assert signals.exit.all()


def test_parquet_age_uses_official_ipo_date_and_cache_tracks_corrections(tmp_path):
    root = tmp_path / "daily"
    root.mkdir()
    df = panel().drop("listing_age_days")
    for day, group in df.partition_by("date", as_dict=True).items():
        part = root / f"date={day[0]}"
        part.mkdir()
        group.write_parquet(part / "data.parquet")
    inst = pl.DataFrame({
        "symbol": ["600001.SH"], "name": ["正常公司"], "listing_date": ["2024-01-03"],
    })
    history = pl.DataFrame({
        "symbol": ["600001.SH"], "valid_from": [date(2024, 2, 1)],
        "valid_to": [None], "is_risk_warning": [False], "is_listed": [True],
        # Older status backfills used first available bar here, not the IPO date.
        "listing_date": [date(2024, 2, 1)],
    })
    kwargs = dict(
        field_columns={"listing_age_days"}, instruments=inst,
        instrument_history=history, cache_root=tmp_path / "cache",
    )
    first = load_market_data_matrix_from_parquet(
        root, date(2024, 2, 1), date(2024, 2, 5), **kwargs,
    )
    assert first.field("listing_age_days")[:, 0].tolist() == [29, 30, 31, 32, 33]
    kwargs["instruments"] = inst.with_columns(pl.lit("2024-01-02").alias("listing_date"))
    corrected = load_market_data_matrix_from_parquet(
        root, date(2024, 2, 1), date(2024, 2, 5), **kwargs,
    )
    assert corrected.field("listing_age_days")[0, 0] == 30


def test_live_polars_exit_priority_keeps_exit_notifications():
    df = panel().with_columns(
        pl.lit(True).alias("signal_ma20_breakdown"),
    )
    engine = StrategyEngine([])
    engine._strategies["test"] = StrategyDef(
        meta={"id": "test", "name": "test", "asset_types": ["stock"], "timeframes": ["1d"]},
        basic_filter={}, entry_signals=[], exit_signals=["signal_ma20_breakdown"],
        stop_loss=None, trailing_stop=None, trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None, max_hold_days=None,
        filter_fn=lambda df, params: pl.lit(True), filter_history_fn=None,
        lookback_days=1, source="custom",
    )
    result = engine.run("test", StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=date(2024, 2, 5), current=df.tail(1),
    ))
    assert result.total == 0
    assert result.entry_signal_hits == []
    assert result.exit_signal_hits == [
        {"symbol": "600001.SH", "signals": ["signal_ma20_breakdown"]},
    ]


@pytest.mark.parametrize("cached", [False, True])
def test_absent_listing_history_is_unknown_in_memory_and_on_disk(tmp_path, cached):
    root = tmp_path / "daily" / "date=2024-02-01"
    root.mkdir(parents=True)
    panel().head(1).drop("is_listed", "instrument_status_known").write_parquet(root / "part.parquet")
    market = load_market_data_matrix_from_parquet(
        root.parent, date(2024, 2, 1), date(2024, 2, 1),
        field_columns={"is_listed", "instrument_status_known"},
        cache_root=tmp_path / "cache" if cached else None,
    )
    assert np.isnan(market.field("is_listed")[0, 0])
    assert market.field("instrument_status_known")[0, 0] == 0
