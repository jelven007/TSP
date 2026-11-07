"""Shared daily SMA ATR contract, independent of the legacy Wilder ATR."""
from datetime import date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.factors.registry import factor_columns_view, get_factor
from app.strategy.scoring import materialize_scoring_columns, scoring_value_expr

FID = "atr14_sma_pct"


def prior(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


@pytest.fixture(autouse=True)
def calendar(monkeypatch):
    from app.strategy import basic_filter
    basic_filter.previous_session.cache_clear()
    monkeypatch.setattr(basic_filter, "published_trading_day", lambda day: day.weekday() < 5)
    yield
    basic_filter.previous_session.cache_clear()


def panel(n=18):
    days = []
    day = date(2026, 10, 1)
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return pl.DataFrame({
        "symbol": ["600001.SH"] * n, "date": days,
        "high": [10.5] * n, "low": [9.5] * n, "close": [10.] * n,
        "open": [10.] * n, "volume": [100.] * n, "raw_close": [20.] * n,
    })


def computed(frame):
    expression = scoring_value_expr(frame.columns, FID)
    assert expression is not None, "Shared factor expression is missing"
    return frame.with_columns(expression.alias(FID))[FID]


def test_registration_is_additive_and_has_daily_ratio_contract():
    spec = get_factor(FID)
    assert spec is not None, "Shared ATR factor is not registered"
    assert spec.label == "ATR14简单均值相对波动"
    assert spec.kind == "virtual" and spec.unit == "ratio"
    assert spec.warmup_bars == 15
    assert spec.dependencies == frozenset({"date", "high", "low", "close"})
    assert factor_columns_view()[-1]["id"] == FID
    assert get_factor("atr_pct").dependencies == frozenset({"close", "atr_14"})


def test_daily_simple_mean_scale_and_materialization():
    frame = panel(15).with_columns(
        pl.when(pl.col("date") == panel(15)["date"][-1]).then(12.).otherwise(pl.col("high")).alias("high"),
        pl.when(pl.col("date") == panel(15)["date"][-1]).then(8.).otherwise(pl.col("low")).alias("low"),
    )
    values = computed(frame)
    assert values.head(14).null_count() == 14
    assert values[-1] == pytest.approx(17 / 140, abs=1e-14)
    scaled = frame.with_columns(pl.col("high", "low", "close") * 7)
    assert computed(scaled)[-1] == pytest.approx(values[-1])
    assert materialize_scoring_columns(frame, {FID})[FID].equals(values)
    from app.backtest.factor import FactorBacktestService
    assert FactorBacktestService._compute_missing_factors(frame, {FID})[FID].equals(values)


def test_float_sum_matches_current_research_python_sum():
    from app.factors.atr import atr14_sma_arrays
    high, low, close = np.array([
        (29.74, 27.43, 27.8), (30.11, 27.61, 28.11), (27.93, 25.93, 26.24),
        (28.86, 25.85, 28.86), (28.3, 25.97, 26.34), (28.5, 24.81, 26.01),
        (26.51, 23.47, 24.14), (26.55, 24.31, 24.36), (24.45, 22.88, 23.57),
        (25.91, 23.41, 25.52), (28.07, 25.05, 27.24), (28.33, 26.72, 27.92),
        (29.56, 27.15, 29.22), (29.58, 27.68, 28.81), (30.55, 28.2, 29.12),
    ]).T
    expected = sum(
        max(float(high[i] - low[i]), float(abs(high[i] - close[i - 1])),
            float(abs(low[i] - close[i - 1])))
        for i in range(1, 15)
    ) / 14.
    atr, _ = atr14_sma_arrays(high, low, close, np.ones(15, dtype=bool))
    assert atr[-1] == expected


@pytest.mark.parametrize("damage", ["missing", "duplicate", "bad_close", "nan", "bad_high", "unknown", "minute"])
def test_invalid_history_returns_missing(damage, monkeypatch):
    frame = panel(15)
    if damage == "missing":
        frame = frame.filter(pl.col("date") != frame["date"][7])
    elif damage == "duplicate":
        frame = pl.concat([frame, frame.slice(7, 1)]).sort("date")
    elif damage == "unknown":
        from app.strategy import basic_filter
        monkeypatch.setattr(basic_filter, "published_trading_day", lambda _: None)
    elif damage == "minute":
        frame = frame.with_columns(pl.lit(date(2026, 10, 1)).alias("date"))
    else:
        name, value = {"bad_close": ("close", 0.), "nan": ("high", float("nan")),
                       "bad_high": ("high", 8.)}[damage]
        frame = frame.with_columns(
            pl.when(pl.col("date") == frame["date"][7]).then(value).otherwise(pl.col(name)).alias(name))
    assert computed(frame).null_count() == frame.height


def test_future_and_other_symbols_cannot_affect_values():
    frame = panel(18)
    expected = computed(frame)
    future = frame.with_columns(
        pl.when(pl.col("date") == frame["date"][-1]).then(999.).otherwise(pl.col("high")).alias("high"))
    assert computed(future).head(17).equals(expected.head(17))
    other = frame.with_columns(pl.lit("600002.SH").alias("symbol"), pl.col("high") + 9)
    mixed = pl.concat([other, frame]).sort("date", "symbol", descending=[True, False])
    result = materialize_scoring_columns(mixed, {FID})
    assert result.filter(pl.col("symbol") == "600001.SH").sort("date")[FID].equals(expected)


def test_gap_recovers_after_exactly_fifteen_fresh_daily_rows():
    frame = panel(31)
    day = frame["date"][28]
    frame = frame.filter(pl.col("date") != frame["date"][13])
    result = materialize_scoring_columns(frame, {FID})
    assert result.filter(pl.col("date") == day)[FID][0] == pytest.approx(.1)


def test_signal_and_formula_consumers_reuse_shared_value():
    from app.factors.dsl import FACTOR_COLUMN, compile_formula
    from app.strategy.custom_signals import materialize_factor_columns
    frame = panel(15)
    signal = {"csg_test": pl.col(FID) > .09}
    materialized = materialize_factor_columns(frame, signal)
    assert materialized.select(signal["csg_test"]).to_series()[-1] is True
    formula = compile_formula(FID)
    assert formula.ok and formula.warmup_bars == 15
    # API trial computes referenced factors before running the DSL transform.
    assert formula.frame_transform(materialized)[FACTOR_COLUMN][-1] == pytest.approx(.1)


def test_matrix_matches_frame_and_rejects_minute_axis():
    from app.backtest.matrix import build_market_data_matrix, matrix_feature
    frame = panel(18)
    market = build_market_data_matrix(frame)
    values = matrix_feature(market, FID)[:, 0]
    np.testing.assert_allclose(values, computed(frame).to_numpy(), rtol=1e-6, equal_nan=True)
    minute = frame.drop("date").with_columns(pl.Series("datetime", [
        datetime(2026, 10, 1, 14, i) for i in range(frame.height)
    ]))
    assert np.isnan(matrix_feature(build_market_data_matrix(minute), FID)).all()


def test_trusted_reference_reuses_core_and_rejects_raw_price_damage():
    from app.strategy import market_data
    getter = getattr(market_data, "get_atr14_sma_reference", None)
    assert getter is not None, "Trusted shared ATR entry is missing"
    frame = panel(15)
    expected = frame["date"].to_list()
    result = getter(frame, ["600001.SH"], expected)
    assert result["atr14_previous_close"][0] == pytest.approx(computed(frame)[-1])
    assert result["atr14"][0] == pytest.approx(1.)
    assert result["atr_reference_date"][0] == expected[-1]
    with pytest.raises(ValueError, match="ATR14"):
        getter(frame.with_columns(pl.lit(0.).alias("raw_close")), ["600001.SH"], expected)
