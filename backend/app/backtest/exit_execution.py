"""Signal-triggered sell orders with a bounded trading-minute window."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import polars as pl

from app.backtest.costs import buy_quantity_rules
from app.backtest.entry_execution import EntryFill, expected_trading_minutes


@dataclass(frozen=True)
class ExitExecution:
    signal_at: datetime | None
    window: tuple[datetime, ...]
    target_shares: int
    filled_shares: int
    cancelled_shares: int
    notional: float
    fee: float
    value: float
    average_price: float | None
    fills: tuple[EntryFill, ...]


def execute_exit_window(
    frame: pl.DataFrame, *, symbol: str, reference_price: float | None,
    quantity: int, sell_value: Callable[[float], float],
    limit_down_price: float | None = None, slippage_bps: float = 0.,
    asset_type: str = "stock",
) -> ExitExecution:
    """Confirm the first downward close crossing, then execute five minute bars.

    Missing bars consume their time slot. No order survives the session.
    Volume is in lots; each fragment uses its open and aggregate fee increment.
    """
    target = max(0, int(quantity))
    signal_at = None
    window = ()
    fills = []
    shares, notional = 0, 0.
    required = {"datetime", "open", "high", "low", "close", "volume"}
    if not frame.is_empty() and required <= set(frame.columns) and (
        reference_price is not None and math.isfinite(reference_price) and reference_price > 0
    ):
        source = frame.filter(pl.col("symbol") == symbol) if "symbol" in frame.columns else frame
        rows = source.to_dicts()
        moments = [row["datetime"] for row in rows]
        if moments != sorted(moments) or len(moments) != len(set(moments)):
            raise ValueError("卖出窗口分钟重复或时间顺序错误")
        previous_above = True
        for row in rows:
            close = row["close"]
            valid = close is not None and math.isfinite(close) and close > 0
            if valid and close < reference_price and previous_above:
                signal_at = row["datetime"]
                break
            previous_above = bool(valid and close >= reference_price)
        if signal_at is not None:
            window = expected_trading_minutes(signal_at + timedelta(minutes=1))
            lookup = {row["datetime"]: row for row in rows}
            _, step, _ = buy_quantity_rules(symbol, asset_type)
            for moment in window:
                if shares == target:
                    break
                row = lookup.get(moment)
                if row is None or not all(
                    row.get(k) is not None and math.isfinite(row[k]) and row[k] > 0
                    for k in ("open", "high", "low", "close", "volume")
                ):
                    continue
                if limit_down_price is not None and row["high"] <= limit_down_price + 1e-9:
                    continue
                volume = row["volume"] * 100
                capacity = math.floor(volume / step + 1e-9) * step
                remaining = target - shares
                # A full residual odd lot may be sold, but cannot exceed capacity.
                quantity_now = min(remaining, capacity)
                if quantity_now <= 0:
                    continue
                price = row["open"] * (1 - slippage_bps / 10000)
                if limit_down_price is not None:
                    price = max(price, limit_down_price)
                before_fee = notional - sell_value(notional) if notional else 0.
                fragment = quantity_now * price
                notional += fragment
                shares += quantity_now
                fee = notional - sell_value(notional)
                fills.append(EntryFill(
                    moment, moment.hour * 60 + moment.minute, quantity_now, price,
                    fragment, fee - before_fee, volume, quantity_now / volume))
    value = sell_value(notional) if shares else 0.
    return ExitExecution(
        signal_at, window, target, shares, target - shares, notional,
        notional - value, value, notional / shares if shares else None, tuple(fills))
