"""隔夜容量撮合的通用汇总委托费用契约。"""
from datetime import datetime, timedelta

import polars as pl
import pytest

from app.backtest.overnight import fill_order, order_fee
from app.strategy.builtin._overnight import DEFAULTS


def minute_frame():
    start = datetime(2026, 9, 4, 14, 47)
    return pl.DataFrame([
        {
            "symbol": "600001.SH",
            "datetime": start + timedelta(minutes=offset),
            "open": 10.,
            "high": 10.,
            "low": 10.,
            "close": 10.,
            "volume": 10.,
            "amount": 10_000.,
        }
        for offset in range(3)
    ])


def test_fixed_transfer_fee_is_charged_once_across_order_fragments():
    params = {
        **DEFAULTS,
        "transfer_fee": 0.,
        "transfer_fee_fixed": 10.,
        "slippage": 0.,
    }
    assert order_fee(1_000, "buy", params) == 15.
    assert order_fee(1_000, "sell", params) == 15.5

    buy = fill_order(
        minute_frame(), "buy", "600001.SH", 10., 887, 889, params,
        budget=3_015.,
    )
    assert buy["shares"] == 300
    assert len(buy["fills"]) == 3
    assert sum(fill["fee"] for fill in buy["fills"]) == pytest.approx(15.)
    assert buy["value"] == pytest.approx(3_015.)
