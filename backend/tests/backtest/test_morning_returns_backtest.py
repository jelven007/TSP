"""A real liquidation window after stop time, retaining partial-fill economics."""
from datetime import datetime, timedelta

import polars as pl
import pytest
from test_minute_backtest import _minute_frame
from test_morning_backtest import (
    BUY,
    RESUME,
    SECOND,
    SELL,
    SYMBOL,
    configuration,
    full_session,
    service_fixture,
    suspension_service,
)

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


def next_minute_run(tmp_path, data, **patch):
    return run(tmp_path, data, initial_capital=1000000, max_exposure_pct=.01,
               exit_fill="signal_next_minute", **patch)


def extended_frame():
    return pl.concat([frame(), _minute_frame(SELL, [
        (SYMBOL, f"10:{minute}", 10.4) for minute in range(46, 52)
    ])])


def test_next_minute_sell_uses_open_slippage_capacity_and_aggregate_fees(tmp_path):
    data = volume(extended_frame(), {"10:30": 10000., "10:31": 1., "10:35": 3.})
    data = data.with_columns(pl.lit(10.5).alias("open"), pl.lit(10.51).alias("high"))
    result = next_minute_run(tmp_path, data, slippage_bps=10)
    assert not result.error, result.error
    sells = [fill for fill in result.fills if fill["side"] == "sell"]
    assert [f["datetime"][-8:] for f in sells] == ["10:31:00", "10:35:00"]
    assert [f["shares"] for f in sells] == [100, 300]
    assert [f["price"] for f in sells] == pytest.approx([10.4895] * 2)
    assert sum(f["fee"] for f in sells) == pytest.approx(7.0979)
    assert result.config["minute_exit_fill"] == "signal_next_minute"
    assert result.strategy_info["minute_exit_fill"] == "signal_next_minute"


def test_next_minute_missing_bar_is_skipped_within_the_original_deadline(tmp_path):
    data = volume(extended_frame(), {"10:31": 1., "10:35": 1., "10:36": 10000., "10:46": 2.})
    data = data.filter(pl.col("datetime").dt.strftime("%H:%M") != "10:32")
    result = next_minute_run(tmp_path, data)
    assert not result.error, result.error
    assert [f["datetime"][-8:] for f in result.fills if f["side"] == "sell"] == [
        "10:31:00", "10:35:00", "10:36:00"]
    sells = [hit for hit in result.signals if hit["side"] == "sell"]
    assert [(hit["reason"], hit["available_qty"]) for hit in sells] == [
        ("stop_time", 400)]


def test_next_minute_fallback_never_gets_an_extra_capacity_window(tmp_path):
    result = next_minute_run(tmp_path, volume(extended_frame(), {"10:45": 10000., "10:50": 4.}))
    assert result.error and "10:45" in result.error
    assert not result.trades
    assert not [fill for fill in result.fills if fill["side"] == "sell"]
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 400


def test_next_minute_continues_remainder_without_retriggering(tmp_path):
    data = extended_frame().with_columns(*[
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "10:00")
        .then(10.6).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")])
    result = next_minute_run(tmp_path, volume(data, {
        "10:00": 10000., "10:01": 1., "10:06": 10000., "10:31": 1., "10:46": 2.,
    }))
    assert not result.error, result.error
    assert [f["datetime"][-8:] for f in result.fills if f["side"] == "sell"] == [
        "10:01:00", "10:06:00"]
    assert [(hit["reason"], hit["available_qty"]) for hit in result.signals if hit["side"] == "sell"] == [
        ("rise", 400)]


def test_next_minute_expiry_retains_cash_fees_and_remaining_shares(tmp_path):
    data = volume(extended_frame(), {"10:31": 1., "10:50": 1., "10:51": 10000.})
    result = next_minute_run(tmp_path, data)
    assert result.error and "10:45" in result.error and "余股" in result.error
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300
    assert not result.trades and not result.equity_curve
    assert [f["datetime"][-8:] for f in result.fills if f["side"] == "sell"] == ["10:31:00"]
    assert 1000000 + sum(f["notional"] - f["fee"] if f["side"] == "sell"
                        else -f["notional"] - f["fee"] for f in result.fills) == pytest.approx(
                            result.stats["execution"]["cash"])


def test_next_minute_deadline_is_enforced_even_without_final_bar(tmp_path):
    data = volume(extended_frame(), {"10:31": 1., "10:51": 10000.}).filter(
        pl.col("datetime").dt.strftime("%H:%M") != "10:50")
    result = next_minute_run(tmp_path, data)
    assert result.error and "10:45" in result.error and "余股" in result.error
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300


def test_next_minute_duplicate_window_bar_is_explicitly_rejected(tmp_path):
    data = volume(extended_frame(), {"10:31": 1., "10:35": 3.})
    data = pl.concat([data, data.filter(pl.col("datetime").dt.strftime("%H:%M") == "10:32")])
    result = next_minute_run(tmp_path, data)
    assert result.error and "重复" in result.error
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300


def test_next_minute_locked_limit_down_cannot_use_unavailable_open(tmp_path):
    data = volume(extended_frame(), {"10:31": 4., "10:35": 4.}).with_columns(*[
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "10:31")
        .then(9.).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")])
    result = next_minute_run(tmp_path, data)
    assert not result.error, result.error
    assert result.fills[-1]["datetime"] == f"{SELL} 10:35:00"
    assert result.stats["execution"]["sell_limit_down"] == 1


@pytest.mark.parametrize("publication,first_minute", [
    ("11:10:00", "11:11"), ("11:10:45", "11:11"), ("12:30:00", "13:01"),
])
def test_next_minute_resume_uses_original_publication_without_double_delay(tmp_path, publication, first_minute):
    published = datetime.fromisoformat(f"{RESUME}T{publication}")
    first = datetime.fromisoformat(f"{RESUME}T{first_minute}:00")
    events = pl.DataFrame({
        "symbol": [SYMBOL, SYMBOL], "effective_date": [SELL, RESUME],
        "announced_on": [BUY, RESUME],
        "announced_at": [datetime(2026, 9, 4, 19, 54), published],
        "is_suspended": [True, False],
        "source_url": ["https://example.test/halt", "https://example.test/resume"],
    })
    result = suspension_service(tmp_path, events=events, resumed=full_session().filter(
        pl.col("datetime") >= first,
    )).run(configuration(
        end=RESUME, symbols=[SYMBOL, SECOND], exit_fill="signal_next_minute",
        initial_capital=1000000, max_exposure_pct=.01,
    ))
    assert not result.error, result.error
    assert result.trades[0]["exit_date"] == f"{RESUME} {first_minute}"
    assert result.trades[0]["exit_signal_date"] == published.isoformat()


def test_partial_stop_liquidation_finishes_in_following_real_minutes(tmp_path):
    result = run(tmp_path, volume(frame(), {"10:30": 10000., "10:31": 1., "10:32": 1., "10:33": 2.}))
    assert not result.error, result.error
    sells = [fill for fill in result.fills if fill["side"] == "sell"]
    assert [fill["shares"] for fill in sells] == [100, 100, 200]
    assert [fill["datetime"][-8:] for fill in sells] == ["10:31:00", "10:32:00", "10:33:00"]
    assert sum(fill["fee"] for fill in sells) == pytest.approx(5. + 400 * 10.39 * .0005)
    assert result.trades[0]["exit_date"] == f"{SELL} 10:33"
    assert result.stats["execution"]["remaining_positions"] == []


def test_fallback_minute_capacity_is_unavailable(tmp_path):
    result = run(tmp_path, volume(frame(), {"10:45": 4.}))
    assert result.error and "10:45" in result.error
    assert not result.trades
    assert not [fill for fill in result.fills if fill["side"] == "sell"]
    assert not any(hit["reason"] == "fallback" for hit in result.signals if hit["side"] == "sell")


def test_remaining_at_fallback_keeps_actual_cash_and_blocks_later_entries(tmp_path):
    data = pl.concat([frame(), _minute_frame(SELL, [(SYMBOL, "14:30", 10.4)])])
    result = run(tmp_path, volume(data, {"10:31": 1., "10:45": 1., "14:30": 10000.}))
    assert result.error and "10:45" in result.error and "余股" in result.error
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300
    assert not result.trades and not result.equity_curve
    assert len(result.fills) == 2
    assert sum(fill["notional"] - fill["fee"] if fill["side"] == "sell"
               else -fill["notional"] - fill["fee"] for fill in result.fills) + 10000 == pytest.approx(
                   result.stats["execution"]["cash"])


def test_missing_intermediate_minute_is_not_filled_but_later_real_capacity_is_usable(tmp_path):
    data = volume(frame(), {"10:30": 1., "10:32": 10000.}).filter(
        pl.col("datetime").dt.strftime("%H:%M") != "10:31")
    result = run(tmp_path, data)
    assert not result.error, result.error
    assert not result.stats["execution"]["remaining_positions"]
    assert len(result.fills) == 2
    assert result.fills[-1]["datetime"] == f"{SELL} 10:32:00"


def test_early_partial_exit_keeps_one_signal_and_aggregate_fees(tmp_path):
    data = frame().with_columns(*[
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "10:00")
        .then(10.6).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")])
    result = run(tmp_path, volume(data, {"10:01": 1., "10:30": 1., "10:44": 2.}))
    assert not result.error, result.error
    sells = [hit for hit in result.signals if hit["side"] == "sell"]
    assert [(hit["reason"], hit["available_qty"]) for hit in sells] == [
        ("rise", 400)]
    assert [f["shares"] for f in result.fills if f["side"] == "sell"] == [100, 100, 200]
    assert sum(f["fee"] for f in result.fills if f["side"] == "sell") == pytest.approx(5. + 400 * 10.39 * .0005)


@pytest.mark.parametrize("price,reason", [(10.6, "rise"), (9.8, "fall")])
def test_early_exit_follows_current_raw_return_not_high_low_range(tmp_path, price, reason):
    data = frame().with_columns(*[
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "10:00")
        .then(price).otherwise(pl.col(column)).alias(column)
        for column in ("open", "high", "low", "close")
    ])
    result = run(tmp_path, data)
    assert not result.error, result.error
    assert result.trades[0]["exit_signal_date"].endswith("10:00:00")
    assert result.trades[0]["exit_date"] == f"{SELL} 10:01"
    assert result.trades[0]["exit_price"] == pytest.approx(10.39)
    assert result.signals[1]["reason"] == reason
