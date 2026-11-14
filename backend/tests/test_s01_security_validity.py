"""Current S01 market eligibility and frozen definitions must stay independent."""
from datetime import datetime

import polars as pl
import pytest
from test_s01_entry_timing import timed
from test_s01_optional_triggers import context, optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import BUILTINS, bars, daily

from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine
from app.strategy.s01_signals import signal_parameters, timed_entry

RULE = "price_amount_prev_close_v2"


def current(**values):
    return timed(**{"market_validity": RULE, **values})


def evaluate(frame=None, previous=None, signal=None):
    ctx = context(end=890)
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    if previous is not None:
        ctx.daily_history = previous
    result = timed_entry(signal or current(), bars() if frame is None else frame, ctx, {})
    return result, ctx.intraday_data_quality["csgi_" + ENTRY["id"]]


@pytest.mark.parametrize("field", ["open", "high", "low", "close", "amount"])
@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -1.])
def test_every_observed_price_and_amount_must_be_finite_and_valid(field, value):
    frame = bars().with_columns(
        pl.when(pl.col("datetime") == datetime(2026, 8, 25, 10))
        .then(pl.lit(value, dtype=pl.Float64)).otherwise(pl.col(field)).alias(field))
    assert evaluate(frame)[1]["market_valid_count"] == 0


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
def test_zero_price_is_invalid(field):
    assert evaluate(bars().with_columns(pl.lit(0.).alias(field)))[1]["market_valid_count"] == 0


@pytest.mark.parametrize("volume", [None, float("nan"), float("inf"), -1., 0.])
def test_zero_amount_and_invalid_volume_still_count_as_market_valid(volume):
    frame = bars().with_columns(pl.lit(0.).alias("amount"),
                               pl.lit(volume, dtype=pl.Float64).alias("volume"))
    result, quality = evaluate(frame)
    assert quality["market_valid_count"] == quality["market_positive_count"] == 1
    assert quality["market_positive_ratio"] == 1
    assert result.is_empty()  # The individual cumulative-amount threshold still applies.


def test_volume_older_history_and_daily_features_do_not_define_market_validity():
    frame = bars().drop("volume")
    previous = daily().tail(1).select("symbol", "date", "raw_close")
    assert evaluate(frame, previous)[1]["market_valid_count"] == 1


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), 0., -1.])
def test_previous_session_close_must_be_positive_and_finite(value):
    previous = daily().with_columns(pl.lit(value, dtype=pl.Float64).alias("raw_close"))
    assert evaluate(previous=previous)[1]["market_valid_count"] == 0


def test_stale_daily_close_cannot_replace_the_exact_previous_session():
    assert evaluate(previous=daily().head(1))[1]["market_valid_count"] == 0


def test_existing_sparse_minutes_count_without_inventing_missing_bars():
    result, quality = evaluate(bars().tail(1))
    assert quality["market_valid_count"] == 1
    assert result.is_empty()  # Existing individual complete-window check remains.
    assert evaluate(pl.DataFrame())[1]["market_valid_count"] == 0


def test_future_and_uncompleted_bad_bars_never_enter_market_validation():
    future = bars(end=890).with_columns(
        pl.when(pl.col("datetime") > datetime(2026, 8, 25, 14, 45))
        .then(-1.).otherwise(pl.col("amount")).alias("amount"))
    assert evaluate(future)[1]["market_valid_count"] == 1
    ctx = context(end=890)
    ctx.completed_through = datetime(2026, 8, 25, 14, 44)
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    timed_entry(current(), future, ctx, {})
    assert ctx.intraday_data_quality["csgi_" + ENTRY["id"]]["market_valid_count"] == 1


def test_legacy_six_and_eight_parameter_snapshots_keep_their_original_denominator():
    frame = bars().tail(1)
    assert evaluate(frame, signal=timed())[1]["market_valid_count"] == 0
    assert signal_parameters(ENTRY) == ENTRY["params"]
    assert signal_parameters(timed()) == timed()["params"]
    assert signal_parameters(current())["market_validity"] == RULE
    with pytest.raises(ValueError):
        custom_signals.validate(current(market_validity="unknown"))


def test_actual_engine_uses_new_denominator_and_keeps_individual_amount_filter():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=890)
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    second = bars(symbol="600002.SH", price=10.25).with_columns(
        pl.lit(0.).alias("amount"), pl.lit(None, dtype=pl.Float64).alias("volume"))
    ctx.history = pl.concat([ctx.history, second])
    ctx.daily_history = pl.concat([daily(), daily().with_columns(pl.lit("600002.SH").alias("symbol"))])
    with custom_signals.private_signal_registry([current(market_min_valid=2), EXIT]):
        result = e.run("s01", ctx, params={"market_validity": "unknown"})
    quality = result.entry_data_quality["csgi_" + ENTRY["id"]]
    assert quality["market_valid_count"] == quality["market_positive_count"] == 2
    assert [row["symbol"] for row in result.rows] == ["600001.SH"]


def test_market_up_ratio_counts_only_positive_returns_with_all_valid_securities():
    second = bars(symbol="600002.SH", price=9.9).with_columns(pl.lit(0.).alias("amount"))
    previous = pl.concat([daily(), daily().with_columns(pl.lit("600002.SH").alias("symbol"))])
    _, quality = evaluate(pl.concat([bars(), second]), previous, current(market_min_valid=2))
    assert quality["market_valid_count"] == 2
    assert quality["market_positive_count"] == 1
    assert quality["market_positive_ratio"] == .5
    assert quality["market_passed"] is False
