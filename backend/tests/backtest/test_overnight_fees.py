"""隔夜容量撮合按双向汇总委托累计固定过户费。"""
from datetime import datetime, timedelta

import polars as pl
import pytest

from app.backtest.overnight import fill_order, order_fee
from app.strategy.builtin._overnight import DEFAULTS, parameters


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


@pytest.fixture
def fixed_params():
    return {**DEFAULTS, "transfer_fee": 0., "transfer_fee_fixed": 10., "slippage": 0.}


@pytest.mark.parametrize(("side", "expected"), [("buy", 15.), ("sell", 15.5)])
def test_fixed_transfer_fee_is_charged_in_each_direction(fixed_params, side, expected):
    assert order_fee(1_000., side, fixed_params) == pytest.approx(expected)
    assert order_fee(0., side, fixed_params) == 0.


def test_buy_budget_reserves_one_fixed_fee_across_fragments(fixed_params):
    buy = fill_order(
        minute_frame(), "buy", "600001.SH", 10., 887, 889, fixed_params,
        budget=3_015.,
    )
    assert buy["shares"] == 300
    assert len(buy["fills"]) == 3
    assert [fill["fee"] for fill in buy["fills"]] == pytest.approx([15., 0., 0.])
    assert buy["fee"] == pytest.approx(15.)
    assert buy["value"] == pytest.approx(3_015.)


def test_sell_fragments_and_continuation_share_one_fixed_fee(fixed_params):
    frame = minute_frame()
    first = fill_order(frame, "sell", "600001.SH", 10., 887, 887, fixed_params, quantity=300)
    rest = fill_order(
        frame, "sell", "600001.SH", 10., 888, 889, fixed_params,
        quantity=first["remaining"], prior_notional=first["notional"],
    )
    assert first["shares"] + rest["shares"] == 300
    assert first["remaining"] == 200 and rest["remaining"] == 0
    assert [fill["fee"] for fill in first["fills"] + rest["fills"]] == pytest.approx([15.5, .5, .5])
    assert first["fee"] + rest["fee"] == pytest.approx(16.5)
    assert first["value"] + rest["value"] == pytest.approx(2_983.5)


def test_fixed_fee_is_not_charged_without_a_fill(fixed_params):
    result = fill_order(
        minute_frame(), "buy", "600001.SH", 10., 887, 889, fixed_params, budget=1_014.,
    )
    assert result["fills"] == [] and result["shares"] == 0
    assert result["fee"] == result["value"] == 0.


@pytest.mark.parametrize("value", [-1., float("nan"), float("inf"), True, "invalid"])
def test_invalid_fixed_transfer_amount_is_rejected(value):
    with pytest.raises(ValueError):
        parameters({"transfer_fee_fixed": value})


def test_legacy_rate_fee_is_unchanged_when_fixed_amount_is_absent():
    legacy = parameters({})
    assert "transfer_fee_fixed" not in legacy
    assert order_fee(1_000., "buy", legacy) == pytest.approx(5.01)
    assert order_fee(1_000., "sell", legacy) == pytest.approx(5.51)


def test_fixed_fee_normalizes_numeric_configuration():
    assert parameters({"transfer_fee_fixed": "10"})["transfer_fee_fixed"] == 10.
