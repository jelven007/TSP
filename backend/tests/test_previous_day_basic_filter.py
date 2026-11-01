"""Numeric filters use the exact previous exchange session without changing prices."""
from datetime import date

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_basic_filter_mask, build_market_data_matrix
from app.strategy import config
from app.strategy.engine import StrategyEngine


def panel():
    return pl.DataFrame([
        {"symbol": symbol, "date": day, "name": symbol, "open": price, "high": price,
         "low": price, "close": price, "raw_close": price, "volume": 100.0,
         "total_shares": 1e8, "float_shares": 0.5e8, "amount": amount,
         "turnover_rate": turnover, "change_pct": change, "listing_age_days": age}
        for symbol, day, price, amount, turnover, change, age in [
            ("600000.SH", date(2026, 10, 22), 8.0, 1e8, 2.0, -0.023, 90),
            ("600000.SH", date(2026, 10, 23), 10.0, 2e8, 3.0, -0.012, 91),
            ("600000.SH", date(2026, 10, 26), 100.0, 9e8, 10.0, 0.10, 94),
            ("000001.SZ", date(2026, 10, 22), 10.0, 2e8, 3.0, -0.012, 91),
            ("000001.SZ", date(2026, 10, 26), 10.0, 2e8, 3.0, -0.012, 95),
        ]
    ])


BF = {"enabled": True, "data_reference": "previous_trade_day", "price_min": 9.0, "price_max": 11.0,
      "market_cap_min": 9e8, "float_cap_max": 6e8, "amount_min": 1.5e8,
      "turnover_max": 4.0, "change_pct_min": -1.3, "change_pct_max": -1.1,
      "exclude_new_days": 91}


def test_previous_friday_values_filter_without_overwriting_current_prices():
    history = panel()
    current = history.filter(pl.col("date") == date(2026, 10, 26))
    result = StrategyEngine._apply_basic_filter(current, BF, history=history, as_of=date(2026, 10, 26))
    assert result["symbol"].to_list() == ["600000.SH"]
    assert result["close"].to_list() == [100.0]
    assert result.columns == current.columns
    assert StrategyEngine._apply_basic_filter(current, {**BF, "enabled": False}).height == 2
    assert StrategyEngine._apply_basic_filter(current, {"price_max": 11}).height == 1
    assert StrategyEngine._apply_basic_filter(current, BF, as_of=date(2026, 10, 26)).is_empty()


def test_matrix_matches_dataframe_and_never_reuses_stale_symbol_row():
    history = panel()
    market = build_market_data_matrix(history, field_columns=set(history.columns))
    result = build_basic_filter_mask(market, BF)
    assert result[-1].tolist() == [False, True]
    np.testing.assert_allclose(market.close[-1], [10.0, 100.0])
    # No previous day in the market axis cannot silently mean "last observed".
    sparse = history.filter(pl.col("date") != date(2026, 10, 23))
    assert not build_basic_filter_mask(build_market_data_matrix(sparse, field_columns=set(sparse.columns)), BF)[-1].any()


def test_holiday_previous_day_is_september_30():
    history = panel().filter(pl.col("symbol") == "600000.SH").head(2).with_columns(
        pl.Series("date", [date(2026, 9, 30), date(2026, 10, 8)]))
    result = StrategyEngine._apply_basic_filter(history.tail(1), {
        "data_reference": "previous_trade_day", "price_max": 9,
    }, history=history, as_of=date(2026, 10, 8))
    assert result.height == 1
    assert result["close"][0] == 10


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -50.1, 50.1, 0.11, "1"])
def test_percentage_validation_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        config.validate_basic_filter({"change_pct_min": value})


def test_percentage_and_reference_persist_and_reject_reversed_range(tmp_path):
    config.save_override(tmp_path, "test", {"basic_filter": {
        "data_reference": "previous_trade_day", "change_pct_min": -50, "change_pct_max": 50.0}})
    assert config.load_override(tmp_path, "test")["basic_filter"]["change_pct_min"] == -50
    for bf in [{"change_pct_min": 1, "change_pct_max": -1}, {"data_reference": "future"}]:
        with pytest.raises(ValueError):
            config.validate_basic_filter(bf)


def test_each_bound_rejects_missing_prior_data_in_matrix():
    history = panel().filter(pl.col("date") == date(2026, 10, 26))
    market = build_market_data_matrix(history, field_columns=set(history.columns))
    for stem in ["price", "market_cap", "float_cap", "amount", "turnover", "change_pct"]:
        assert not build_basic_filter_mask(market, {
            "data_reference": "previous_trade_day", stem + "_max": 50,
        }).any()


def test_matrix_derives_daily_return_when_only_base_prices_are_stored():
    history = panel().filter(pl.col("symbol") == "600000.SH").drop("change_pct")
    market = build_market_data_matrix(history, field_columns=set(history.columns))
    # Friday closes at 10 after Thursday's 8: Friday return is +25%.
    result = build_basic_filter_mask(market, {
        "data_reference": "previous_trade_day", "change_pct_min": 24.9, "change_pct_max": 25.1})
    assert result[-1, 0]


def test_engine_loads_history_and_uses_prior_values_for_actual_daily_run():
    from test_strategy_saved_params import _make_engine

    from app.strategy.engine import StrategyDataContext

    engine, _ = _make_engine()
    strategy = engine.get("saved_params")
    strategy.filter_fn = lambda _df, _params: pl.lit(True)
    history = panel()
    day = date(2026, 10, 26)
    overrides = {"basic_filter": BF}
    assert engine.required_history_bars(["saved_params"], overrides_map={"saved_params": overrides}) == 2
    context = StrategyDataContext("stock", "1d", day,
        current=history.filter(pl.col("date") == day), history=history)
    result = engine.run("saved_params", context, overrides=overrides)
    assert [row["symbol"] for row in result.rows] == ["600000.SH"]
    assert result.rows[0]["close"] == 100


def test_minute_candidates_use_daily_previous_close_not_last_minute():
    from test_strategy_saved_params import _make_engine

    from app.strategy.engine import StrategyDataContext

    engine, _ = _make_engine()
    strategy = engine.get("saved_params")
    strategy.execution_backend = "minute_filter"
    strategy.filter_fn = None
    strategy.meta["timeframes"] = ["1m"]
    strategy.filter_minute_history_fn = lambda df, _params: df
    history = panel()
    day = date(2026, 10, 26)
    current = history.filter(pl.col("date") == day)
    assert engine.minute_daily_history_bars(["saved_params"],
        overrides_map={"saved_params": {"basic_filter": BF}}) == 2
    context = StrategyDataContext("stock", "1m", day, current=current,
        history=current, daily_history=history)
    result = engine.run("saved_params", context, overrides={"basic_filter": BF})
    assert [row["symbol"] for row in result.rows] == ["600000.SH"]
    assert result.rows[0]["close"] == 100
