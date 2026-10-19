"""Shared filtering/scoring must use tradable prices and complete factor inputs."""
from datetime import date
from types import SimpleNamespace

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    build_basic_filter_mask,
    build_market_data_matrix,
    make_signal_matrix,
)
from app.backtest.strategy import StrategyBacktestService, _basic_filter_dependencies
from app.strategy.engine import StrategyEngine


def _panel():
    return pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH", "688001.SH"],
        "date": [date(2024, 1, 2)] * 3,
        "open": [5.0, 5.0, 5.0],
        "high": [5.1, 5.1, 5.1],
        "low": [4.9, 4.9, 4.9],
        "close": [5.0, 5.0, 5.0],
        "raw_close": [10.0, 20.0, None],
        "volume": [1000.0] * 3,
        "total_shares": [100.0, 50.0, 100.0],
        "float_shares": [50.0, 25.0, 50.0],
    })


@pytest.mark.parametrize("bounds", [
    {"price_min": 8.0, "price_max": 15.0},
    {"market_cap_min": 900.0, "market_cap_max": 1100.0},
    {"float_cap_min": 450.0, "float_cap_max": 550.0},
])
def test_absolute_price_and_capitalization_use_raw_price_in_both_paths(bounds):
    panel = _panel()
    expected = [True, "price_min" not in bounds, False]
    expr = StrategyEngine._basic_filter_expr(panel, bounds)
    assert panel.select(expr.fill_null(False)).to_series().to_list() == expected
    market = build_market_data_matrix(
        panel, field_columns={"raw_close", "total_shares", "float_shares"},
    )
    assert build_basic_filter_mask(market, bounds)[0].tolist() == expected
    assert "raw_close" in _basic_filter_dependencies(bounds)


def test_legacy_frame_without_raw_price_retains_close_fallback():
    panel = _panel().drop("raw_close")
    config = {"price_min": 4.0, "price_max": 6.0}
    assert StrategyEngine._apply_basic_filter(panel, config).height == 3
    market = build_market_data_matrix(panel)
    assert build_basic_filter_mask(market, config).all()


@pytest.mark.parametrize("raw_price", [None, float("nan"), float("inf")])
def test_invalid_raw_price_is_not_replaced_with_adjusted_price(raw_price):
    panel = _panel().with_columns(pl.lit(raw_price, dtype=pl.Float64).alias("raw_close"))
    config = {"market_cap_min": 1.0}
    assert StrategyEngine._apply_basic_filter(panel, config).is_empty()
    market = build_market_data_matrix(panel, field_columns={"raw_close", "total_shares"})
    assert not build_basic_filter_mask(market, config).any()


def test_enabled_capitalization_bound_rejects_absent_shares():
    panel = _panel().drop("total_shares")
    config = {"market_cap_min": 1.0}
    assert StrategyEngine._apply_basic_filter(panel, config).is_empty()
    market = build_market_data_matrix(panel, field_columns={"raw_close"})
    assert not build_basic_filter_mask(market, config).any()


class _AllCandidates:
    def compute_signals(self, market, params):
        return make_signal_matrix(
            market.shape,
            entry=np.ones(market.shape, dtype=np.uint8),
            exit=np.array([[0, 0, 1]], dtype=np.uint8),
        )


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_missing_factor_rejects_entry_but_keeps_real_zero_score_and_exit(missing):
    panel = _panel().with_columns(
        pl.Series("factor_a", [1.0, 999.0, 3.0]),
        pl.Series("factor_b", [2.0, missing, 2.0], dtype=pl.Float64),
    )
    weights = {"factor_a": 0.5, "factor_b": 0.5}
    market = build_market_data_matrix(panel, field_columns=set(weights))
    signals = MatrixStrategyPipeline().run(
        _AllCandidates(), market, {},
        MatrixPipelineConfig({}, weights, "score", True),
    )
    assert signals.entry.tolist() == [[1, 0, 0]]
    assert signals.exit.tolist() == [[0, 0, 1]]
    # B must also be absent from normalization; A is the sole eligible entry.
    assert signals.score[0, 0] == 50.0

    candidates = panel.head(2)
    realtime = StrategyEngine._apply_scoring(candidates, weights)
    assert realtime["symbol"].to_list() == ["000001.SZ"]
    assert realtime["score"].to_list() == [50.0]
    strategy = SimpleNamespace(meta={"scoring": weights, "order_by": "score"})
    scored = StrategyBacktestService._apply_score(
        panel, strategy, None, universe_mask=pl.Series([True, True, False]),
    )
    assert scored.height == panel.height  # prices/exit rows survive
    assert scored["score"][0] == 50.0
    assert scored["score"][1] is None

    valid = candidates.with_columns(pl.Series("factor_b", [1.0, 2.0]))
    assert StrategyEngine._apply_scoring(valid, weights)["score"].to_list() == [0.0, 100.0]


def test_wholly_missing_scoring_dependency_is_explicit_error():
    with pytest.raises(ValueError, match="评分"):
        StrategyEngine._apply_scoring(_panel(), {"unknown_factor": 1.0})
    strategy = SimpleNamespace(meta={"scoring": {"unknown_factor": 1.0}})
    with pytest.raises(ValueError, match="评分"):
        StrategyBacktestService._apply_score(_panel(), strategy, None)
