"""Fixed UTC+8 clocks and Polars zone names must preserve Beijing trading days."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from app.market_time import CN_TZ
from app.strategy import custom_signals
from app.strategy.basic_filter import previous_session
from app.strategy.intraday_features import build_feature_frame
from app.strategy.late_session_strength import (
    DEFAULT_PARAMS,
    condition_inputs,
    evaluate,
    pending_evaluation_time,
)

DAY = date(2026, 6, 29)
AT = datetime.combine(DAY, time(14, 30))


def signal(**params):
    return {
        "id": "timezone_late", "name": "尾盘走强", "kind": "entry",
        "timeframe": "intraday", "template": "late_session_strength",
        "conditions": [], "enabled": True,
        "params": dict.fromkeys(DEFAULT_PARAMS) | params,
    }


def minutes():
    return pl.DataFrame({
        "symbol": ["a"] * 3,
        "datetime": [AT - timedelta(minutes=2), AT - timedelta(minutes=1), AT],
        "open": [10.0, 10.1, 10.2], "close": [10.0, 10.1, 10.2],
        "high": [10.1, 10.2, 10.3], "low": [9.9, 10.0, 10.1],
        "volume": [100.0] * 3, "amount": [100000.0, 101000.0, 102000.0],
    })


@pytest.mark.parametrize("source_zone", ["UTC", "Asia/Shanghai"])
def test_intraday_features_convert_aware_minutes_before_cutoff(source_zone):
    frame = minutes()
    aware = frame.with_columns(
        pl.col("datetime").dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone(source_zone),
    )
    cutoff = (AT + timedelta(seconds=30)).replace(tzinfo=CN_TZ)
    expected = build_feature_frame(frame, prev_close={"a": 10.0}, cutoff=cutoff)
    actual = build_feature_frame(aware, prev_close={"a": 10.0}, cutoff=cutoff)
    assert_frame_equal(actual, expected)
    assert actual["datetime"][-1] == AT
    assert actual["date"].to_list() == [DAY] * 3
    assert actual["vwap"][-1] == pytest.approx(10.1)


@pytest.mark.parametrize("source_zone", ["UTC", "Asia/Shanghai"])
def test_optional_entry_uses_beijing_bar_for_aware_minutes(source_zone):
    frame = minutes().with_columns(
        pl.col("datetime").dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone(source_zone),
    )
    result = evaluate(
        frame, signal(time="14:30:00", evaluation_delay_minutes=.5, change_min_pct=1),
        day=DAY, pool_symbols=["a"], prev_close={"a": 10.0},
        completed_through=(AT + timedelta(seconds=30)).replace(tzinfo=CN_TZ),
    )
    assert not result.diagnostics
    assert result.symbols == ["a"]
    assert result.progress["bar_at"] == AT.isoformat()


@pytest.mark.parametrize("stamp_at,expected", [
    (None, {"a": 10000.0}),
    (time(15), {"a": 10000.0}),
    (time(23, 59, 59, 999000), {"a": 10000.0}),
    (time(14, 59, 59, 999000), {}),
    (time(0), {}),
])
def test_five_day_volume_reference_checks_snapshot_clock_in_beijing(stamp_at, expected):
    days = []
    prior = DAY
    for _ in range(5):
        prior = previous_session(prior)
        days.append(prior)
    # Real backtest enriched frames carry these columns even for historical batches.
    daily = pl.DataFrame({
        "symbol": ["a"] * 5, "date": days, "volume": [10000.0] * 5,
        "quote_ts": [
            int(datetime.combine(day, stamp_at, CN_TZ).timestamp() * 1000)
            if stamp_at is not None else None for day in days
        ],
        "quote_time_basis": ["source_snapshot" if stamp_at is not None else None] * 5,
    }, schema_overrides={"quote_ts": pl.Int64, "quote_time_basis": pl.Utf8})
    context = SimpleNamespace(
        as_of=DAY, daily_history=daily, current=None, pool_history=None, pool_universe=None,
    )
    assert condition_inputs(context, signal(volume_ratio_min=1)) == {
        "previous_volume_mean": expected,
    }


def test_pending_confirmation_reads_aware_history_when_completed_clock_is_absent(tmp_path):
    custom_signals.save_one(tmp_path, signal(time="14:30:00", evaluation_delay_minutes=.5))
    engine = SimpleNamespace(
        _user_data_dir=lambda: tmp_path,
        _effective_signals=lambda overrides, key, defaults: overrides.get(key, defaults),
    )
    strategy = SimpleNamespace(entry_signals=["csgi_timezone_late"])
    context = SimpleNamespace(
        as_of=DAY, observed_at=AT.replace(tzinfo=CN_TZ), completed_through=None,
        history=minutes().with_columns(
            pl.col("datetime").dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone("UTC"),
        ),
    )
    assert pending_evaluation_time(engine, strategy, {}, context) == AT + timedelta(seconds=30)


def test_s03_signal_injection_accepts_historical_batches_with_quote_columns(tmp_path):
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    sig = signal(
        time="14:30:00", end_time="14:55:00", evaluation_delay_minutes=.5,
        pool_up_min_pct=60, change_min_pct=3, change_max_pct=5,
        turnover_min_pct=5, volume_ratio_min=1.5,
    )
    custom_signals.save_one(tmp_path, sig)
    days = []
    prior = DAY
    for _ in range(5):
        prior = previous_session(prior)
        days.append(prior)
    daily = pl.DataFrame({
        "symbol": ["a"] * 5, "date": days, "volume": [10000.0] * 5,
        "close": [10.0] * 5, "raw_close": [10.0] * 5, "float_shares": [2e7] * 5,
        "quote_ts": [None] * 5, "quote_time_basis": [None] * 5,
    }, schema_overrides={"quote_ts": pl.Int64, "quote_time_basis": pl.Utf8})
    frame = minutes().with_columns(
        pl.lit(10.4).alias("close"), pl.lit(5000.0).alias("volume"),
        pl.lit(5.2e6).alias("amount"),
    )
    current = daily.head(1).with_columns(pl.lit(DAY).alias("date"))
    context = StrategyDataContext(
        "stock", "1m", DAY, history=frame, daily_history=daily, current=current,
        observed_at=AT + timedelta(seconds=30),
    )
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    injected = engine._inject_intraday_signal_columns(
        frame, context=context, pool_symbols=["a"],
        requested_signals={"csgi_timezone_late"},
    )
    assert context.intraday_errors == []
    assert context.reference_errors == {}
    hits = injected.filter(pl.col("csgi_timezone_late"))
    assert hits["symbol"].to_list() == ["a"]
    assert hits["datetime"].to_list() == [AT]
    assert hits["entry_evaluated_at"].to_list() == [AT + timedelta(seconds=30)]
