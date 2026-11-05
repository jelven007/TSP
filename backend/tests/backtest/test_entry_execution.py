from __future__ import annotations

from datetime import datetime, timedelta

import polars as pl
import pytest

from app.backtest.entry_execution import execute_entry_window

START = datetime(2026, 1, 5, 9, 31)


def bars(
    *,
    highs: list[float],
    volumes: list[float],
    lows: list[float] | None = None,
    times: list[datetime] | None = None,
    symbol: str = "600001.SH",
) -> pl.DataFrame:
    times = times or [START + timedelta(minutes=index) for index in range(len(highs))]
    lows = lows or [price - 0.1 for price in highs]
    return pl.DataFrame({
        "symbol": [symbol] * len(highs),
        "datetime": times,
        "open": [float(value) for value in lows],
        "high": [float(value) for value in highs],
        "low": [float(value) for value in lows],
        "close": [float(value) for value in highs],
        "volume": [float(value) for value in volumes],
    })


def buy_value(notional: float) -> float:
    return notional + max(5, notional * 0.0002) + notional * (0.0005 + 0.001)


def test_entry_window_fills_at_each_minute_high_and_cancels_residual():
    result = execute_entry_window(
        bars(
            highs=[10.0, 10.2, 10.1, 10.3, 10.4],
            volumes=[1, 1, 1, 1, 1],
        ),
        symbol="600001.SH",
        start_at=START,
        prior_close=9.5,
        budget=10_000,
        size_order=lambda price: 1_000,
        buy_value=buy_value,
    )

    assert result.target_shares == 1_000
    assert result.filled_shares == 500
    assert result.cancelled_shares == 500
    assert [fill.price for fill in result.fills] == [10.0, 10.2, 10.1, 10.3, 10.4]
    assert result.average_price == pytest.approx(10.2)
    assert sum(fill.fee for fill in result.fills) == pytest.approx(result.fee)
    assert result.status == "partial"


def test_capacity_can_fill_the_whole_order_in_the_first_bar():
    result = execute_entry_window(
        bars(highs=[10.2] * 5, volumes=[10] * 5),
        symbol="600001.SH",
        start_at=START,
        prior_close=10,
        budget=100_000,
        size_order=lambda _: 500,
        buy_value=lambda notional: notional,
    )

    assert result.filled_shares == 500
    assert len(result.fills) == 1
    assert result.fills[0].price == 10.2
    assert result.status == "filled"


def test_zero_volume_and_one_price_limit_up_consume_slots_without_fill():
    result = execute_entry_window(
        bars(
            highs=[10, 11, 10.2, 10.3, 10.4],
            lows=[9.9, 11, 10, 10, 10],
            volumes=[0, 10, 1, 1, 1],
        ),
        symbol="600001.SH",
        start_at=START,
        prior_close=10,
        budget=100_000,
        size_order=lambda _: 500,
        buy_value=lambda notional: notional,
    )

    assert [fill.bar for fill in result.fills] == [573, 574, 575]
    assert result.filled_shares == 300
    assert result.cancelled_shares == 200


def test_authoritative_limit_price_blocks_one_price_risk_warning_bar():
    result = execute_entry_window(
        bars(
            highs=[10.5, 10.4],
            lows=[10.5, 10.3],
            volumes=[10, 10],
        ),
        symbol="600001.SH",
        start_at=START,
        prior_close=10,
        limit_up_price=10.5,
        budget=100_000,
        size_order=lambda _: 100,
        buy_value=lambda notional: notional,
    )

    assert result.filled_shares == 100
    assert result.fills[0].bar == 572
    assert result.fills[0].price == 10.4


def test_missing_bar_consumes_its_slot_and_does_not_extend_the_window():
    frame = bars(
        highs=[10, 10.2, 10.3, 10.4],
        volumes=[1, 1, 1, 1],
        times=[START, START + timedelta(minutes=2), START + timedelta(minutes=3),
               START + timedelta(minutes=4)],
    )

    result = execute_entry_window(
        frame,
        symbol="600001.SH",
        start_at=START,
        prior_close=10,
        budget=100_000,
        size_order=lambda _: 500,
        buy_value=lambda notional: notional,
    )

    assert [fill.bar for fill in result.fills] == [571, 573, 574, 575]
    assert result.filled_shares == 400
    assert result.cancelled_shares == 100


def test_entry_window_skips_lunch_without_spending_window_slots():
    times = [
        datetime(2026, 1, 5, 11, 29),
        datetime(2026, 1, 5, 11, 30),
        datetime(2026, 1, 5, 13, 1),
        datetime(2026, 1, 5, 13, 2),
        datetime(2026, 1, 5, 13, 3),
    ]
    result = execute_entry_window(
        bars(highs=[10] * 5, volumes=[1] * 5, times=times),
        symbol="600001.SH",
        start_at=times[0],
        prior_close=10,
        budget=100_000,
        size_order=lambda _: 500,
        buy_value=lambda notional: notional,
    )

    assert [fill.bar for fill in result.fills] == [689, 690, 781, 782, 783]
    assert result.filled_shares == 500


def test_entry_window_truncates_at_close_and_cancels_the_remainder():
    times = [datetime(2026, 1, 5, 14, 59), datetime(2026, 1, 5, 15, 0)]
    result = execute_entry_window(
        bars(highs=[10, 10.1], volumes=[1, 1], times=times),
        symbol="600001.SH",
        start_at=times[0],
        prior_close=10,
        budget=100_000,
        size_order=lambda _: 500,
        buy_value=lambda notional: notional,
    )

    assert [fill.bar for fill in result.fills] == [899, 900]
    assert result.filled_shares == 200
    assert result.cancelled_shares == 300


def test_aggregate_fee_charges_the_minimum_commission_once():
    result = execute_entry_window(
        bars(highs=[10] * 5, volumes=[1] * 5),
        symbol="600001.SH",
        start_at=START,
        prior_close=10,
        budget=100_000,
        size_order=lambda _: 500,
        buy_value=buy_value,
    )

    expected_fee = max(5, result.notional * 0.0002) + result.notional * (0.0005 + 0.001)
    assert result.fee == pytest.approx(expected_fee)
    assert sum(fill.fee for fill in result.fills) == pytest.approx(expected_fee)


def test_duplicate_or_unsorted_minute_rows_are_rejected():
    duplicate = bars(
        highs=[10, 10.1],
        volumes=[1, 1],
        times=[START, START],
    )
    unsorted = bars(
        highs=[10, 10.1],
        volumes=[1, 1],
        times=[START + timedelta(minutes=1), START],
    )

    with pytest.raises(ValueError, match="重复分钟"):
        execute_entry_window(
            duplicate, symbol="600001.SH", start_at=START, prior_close=10,
            budget=100_000, size_order=lambda _: 500, buy_value=lambda notional: notional,
        )
    with pytest.raises(ValueError, match="时间顺序"):
        execute_entry_window(
            unsorted, symbol="600001.SH", start_at=START, prior_close=10,
            budget=100_000, size_order=lambda _: 500, buy_value=lambda notional: notional,
        )
