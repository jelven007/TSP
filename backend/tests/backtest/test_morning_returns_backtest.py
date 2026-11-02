"""A real liquidation window after stop time, retaining partial-fill economics."""
from datetime import datetime, timedelta

import polars as pl
import pytest
from test_minute_backtest import _minute_frame
from test_morning_backtest import BUY, SELL, SYMBOL, configuration, service_fixture

from app.strategy import custom_signals


def frame():
    start = datetime.combine(SELL, datetime.min.time()).replace(hour=9, minute=31)
    return _minute_frame(SELL, [
        (SYMBOL, (start + timedelta(minutes=i)).strftime("%H:%M"), 10.4)
        for i in range(75)
    ])


def run(tmp_path, data, **patch):
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]), SELL: data,
    })
    custom_signals.save_one(tmp_path, {
        "id": "morning_liquidation", "name": "早盘清仓", "kind": "exit",
        "timeframe": "intraday", "template": "morning_liquidation", "conditions": [],
        "enabled": True, "params": {"time": "10:30:00", "fallback_time": "10:45:00",
                                   "rise_pct": 6, "fall_pct": 2},
    })
    return service.run(configuration(**patch))


def volume(data, amounts):
    return data.with_columns(pl.col("datetime").dt.strftime("%H:%M").replace_strict(
        amounts, default=0., return_dtype=pl.Float64,
    ).alias("volume"))


def test_partial_stop_liquidation_finishes_in_following_real_minutes(tmp_path):
    result = run(tmp_path, volume(frame(), {"10:30": 1., "10:31": 1., "10:32": 2.}))
    assert not result.error, result.error
    sells = [fill for fill in result.fills if fill["side"] == "sell"]
    assert [fill["shares"] for fill in sells] == [100, 100, 200]
    assert [fill["datetime"][-8:] for fill in sells] == ["10:30:00", "10:31:00", "10:32:00"]
    assert sum(fill["fee"] for fill in sells) == pytest.approx(7.08)
    assert result.trades[0]["exit_date"] == f"{SELL} 10:32"
    assert result.stats["execution"]["remaining_positions"] == []


def test_fallback_can_use_its_own_real_minute_capacity(tmp_path):
    result = run(tmp_path, volume(frame(), {"10:45": 4.}))
    assert not result.error, result.error
    assert result.trades[0]["exit_date"] == f"{SELL} 10:45"
    assert result.fills[-1]["shares"] == 400
    assert any(hit["reason"] == "fallback" for hit in result.signals if hit["side"] == "sell")


def test_remaining_at_fallback_keeps_actual_cash_and_blocks_later_entries(tmp_path):
    data = pl.concat([frame(), _minute_frame(SELL, [(SYMBOL, "14:30", 10.4)])])
    result = run(tmp_path, volume(data, {"10:30": 1., "10:45": 1., "14:30": 10000.}))
    assert result.error and "10:45" in result.error and "余股" in result.error
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 200
    assert not result.trades and not result.equity_curve
    assert len(result.fills) == 3
    assert sum(fill["notional"] - fill["fee"] if fill["side"] == "sell"
               else -fill["notional"] - fill["fee"] for fill in result.fills) + 10000 == pytest.approx(
                   result.stats["execution"]["cash"])


def test_missing_intermediate_minute_cannot_be_replaced_by_later_capacity(tmp_path):
    data = volume(frame(), {"10:30": 1., "10:32": 10000.}).filter(
        pl.col("datetime").dt.strftime("%H:%M") != "10:31")
    result = run(tmp_path, data)
    assert result.error and "窗口" in result.error
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300
    assert len(result.fills) == 2


def test_stop_signal_retriggers_only_actual_remainder_of_early_partial_exit(tmp_path):
    data = frame().with_columns(*[
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "10:00")
        .then(10.6).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")])
    result = run(tmp_path, volume(data, {"10:00": 1., "10:30": 1., "10:45": 2.}))
    assert not result.error, result.error
    sells = [hit for hit in result.signals if hit["side"] == "sell"]
    assert [(hit["reason"], hit["available_qty"]) for hit in sells] == [
        ("rise", 400), ("stop_time", 300), ("fallback", 200)]
    assert [f["shares"] for f in result.fills if f["side"] == "sell"] == [100, 100, 200]
    assert sum(f["fee"] for f in result.fills if f["side"] == "sell") == pytest.approx(7.09)


@pytest.mark.parametrize("price,reason", [(10.6, "rise"), (9.8, "fall")])
def test_early_exit_follows_current_raw_return_not_high_low_range(tmp_path, price, reason):
    data = frame().with_columns(*[
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "10:00")
        .then(price).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")
    ])
    result = run(tmp_path, data)
    assert not result.error, result.error
    assert result.trades[0]["exit_date"] == f"{SELL} 10:00"
    assert result.trades[0]["exit_price"] == pytest.approx(price)
    assert result.signals[1]["reason"] == reason
