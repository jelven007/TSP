"""Five-minute, capacity-constrained entry-order execution."""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Literal

import polars as pl

from app.backtest.costs import buy_quantity_rules
from app.price_limits import board_limit_pct


@dataclass(frozen=True)
class EntryFill:
    datetime: datetime
    bar: int
    shares: int
    price: float
    notional: float
    fee: float
    minute_volume_shares: float
    participation: float


@dataclass(frozen=True)
class EntryExecution:
    target_shares: int
    filled_shares: int
    cancelled_shares: int
    notional: float
    fee: float
    value: float
    average_price: float | None
    fills: tuple[EntryFill, ...]
    status: Literal["filled", "partial", "unfilled"]


def expected_trading_minutes(
    start_at: datetime,
    *,
    count: int = 5,
    same_day: bool = True,
) -> tuple[datetime, ...]:
    """Return minute-bar labels at or after ``start_at`` within one A-share session."""
    if count <= 0:
        return ()
    day = start_at.date()
    session: list[datetime] = []
    for start, length in ((time(9, 31), 120), (time(13, 1), 120)):
        first = datetime.combine(day, start, tzinfo=start_at.tzinfo)
        session.extend(first + timedelta(minutes=offset) for offset in range(length))
    result = tuple(value for value in session if value >= start_at)[:count]
    if same_day:
        return result
    return result


def _limit_up_price(symbol: str, prior_close: float) -> float:
    cents = math.floor(prior_close * 100 + 0.5)
    numerator = round((1 + board_limit_pct(symbol)) * 100)
    return ((cents * numerator + 50) // 100) / 100


def _finite_positive(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _valid_price_row(row: dict[str, object]) -> bool:
    return all(_finite_positive(row.get(field)) for field in ("open", "high", "low", "close"))


def _aggregate_fee(buy_value: Callable[[float], float], notional: float) -> float:
    if notional <= 0:
        return 0.0
    value = float(buy_value(notional))
    if not math.isfinite(value) or value < notional:
        raise ValueError("买入费用函数返回无效金额")
    return value - notional


def _affordable_quantity(
    *,
    current_notional: float,
    proposed: int,
    price: float,
    step: int,
    budget: float,
    buy_value: Callable[[float], float],
) -> int:
    if proposed <= 0:
        return 0
    if buy_value(current_notional + proposed * price) <= budget + 1e-9:
        return proposed
    low, high = 0, proposed // step
    while low < high:
        middle = (low + high + 1) // 2
        quantity = middle * step
        if buy_value(current_notional + quantity * price) <= budget + 1e-9:
            low = middle
        else:
            high = middle - 1
    return low * step


def execute_entry_window(
    frame: pl.DataFrame,
    *,
    symbol: str,
    start_at: datetime,
    prior_close: float,
    budget: float,
    size_order: Callable[[float], int],
    buy_value: Callable[[float], float],
    limit_up_price: float | None = None,
    window_minutes: int = 5,
    asset_type: str = "stock",
) -> EntryExecution:
    """Execute one aggregate buy order across at most five trading-minute bars."""
    if not _finite_positive(prior_close):
        raise ValueError("缺少有效上一交易日收盘价")
    if not math.isfinite(float(budget)) or budget < 0:
        raise ValueError("买入预算无效")

    required = {"datetime", "open", "high", "low", "close", "volume"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"分钟K缺少字段: {', '.join(sorted(missing))}")

    source = frame
    if "symbol" in source.columns:
        source = source.filter(pl.col("symbol") == symbol)
    rows = [
        row for row in source.iter_rows(named=True)
        if isinstance(row.get("datetime"), datetime)
        and row["datetime"].date() == start_at.date()
    ]
    datetimes = [row["datetime"] for row in rows]
    if len(datetimes) != len(set(datetimes)):
        raise ValueError("成交窗口含重复分钟")
    if datetimes != sorted(datetimes):
        raise ValueError("成交窗口分钟时间顺序错误")

    window = expected_trading_minutes(start_at, count=window_minutes, same_day=True)
    by_datetime = {row["datetime"]: row for row in rows}
    limit_up = (
        _limit_up_price(symbol, float(prior_close))
        if limit_up_price is None
        else float(limit_up_price)
    )
    if math.isnan(limit_up) or limit_up <= 0:
        raise ValueError("涨停价无效")
    minimum, step, maximum = buy_quantity_rules(symbol, asset_type)

    first_price = next(
        (
            min(limit_up, float(row["high"]))
            for moment in window
            if (row := by_datetime.get(moment)) is not None and _valid_price_row(row)
        ),
        None,
    )
    if first_price is None:
        target = 0
    else:
        target = min(maximum, max(0, int(size_order(first_price))))
        target = target // step * step
        if target < minimum:
            target = 0

    notional = 0.0
    shares = 0
    fills: list[EntryFill] = []
    for moment in window:
        if shares >= target:
            break
        row = by_datetime.get(moment)
        if row is None or not _valid_price_row(row):
            continue
        try:
            volume = float(row["volume"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(volume) or volume <= 0:
            continue
        high = float(row["high"])
        low = float(row["low"])
        if math.isfinite(limit_up) and low >= limit_up - 1e-9 and high >= limit_up - 1e-9:
            continue

        price = min(limit_up, high)
        minute_volume_shares = volume * 100
        capacity = math.floor(minute_volume_shares / step + 1e-9) * step
        proposed = min(target - shares, capacity)
        quantity = _affordable_quantity(
            current_notional=notional,
            proposed=proposed,
            price=price,
            step=step,
            budget=float(budget),
            buy_value=buy_value,
        )
        if quantity <= 0:
            continue

        before_fee = _aggregate_fee(buy_value, notional)
        fragment_notional = quantity * price
        notional += fragment_notional
        shares += quantity
        after_fee = _aggregate_fee(buy_value, notional)
        fills.append(EntryFill(
            datetime=moment,
            bar=moment.hour * 60 + moment.minute,
            shares=quantity,
            price=price,
            notional=fragment_notional,
            fee=after_fee - before_fee,
            minute_volume_shares=minute_volume_shares,
            participation=quantity / minute_volume_shares,
        ))

    fee = _aggregate_fee(buy_value, notional)
    cancelled = max(target - shares, 0)
    status: Literal["filled", "partial", "unfilled"]
    if target > 0 and shares == target:
        status = "filled"
    elif shares > 0:
        status = "partial"
    else:
        status = "unfilled"
    return EntryExecution(
        target_shares=target,
        filled_shares=shares,
        cancelled_shares=cancelled,
        notional=notional,
        fee=fee,
        value=notional + fee,
        average_price=notional / shares if shares else None,
        fills=tuple(fills),
        status=status,
    )
