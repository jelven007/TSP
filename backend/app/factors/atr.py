"""Daily SMA14 true range: one kernel for factor, matrix and minute references."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import polars as pl

FACTOR_ID = "atr14_sma_pct"


def daily_links(days: Sequence[date]) -> np.ndarray:
    """Published daily adjacency; unknown days and intraday axes stay missing."""
    from app.strategy.basic_filter import previous_session, published_trading_day

    links = np.zeros(len(days), dtype=bool)
    for i, day in enumerate(days):
        if type(day) is not date or published_trading_day(day) is not True:
            continue
        if i == 0:
            links[i] = True
            continue
        try:
            links[i] = previous_session(day) == days[i - 1]
        except ValueError:
            links[i] = False
    return links


def atr14_sma_arrays(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, links: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """15 valid rows, 14 TRs, Python 3.12 Float64 compensated sum; no fill."""
    high, low, close = (np.asarray(value, dtype=np.float64) for value in (high, low, close))
    atr = np.full(close.shape, np.nan, dtype=np.float64)
    ratio = np.full(close.shape, np.nan, dtype=np.float64)
    if len(close) < 15:
        return atr, ratio
    valid = (
        np.isfinite(high) & np.isfinite(low) & np.isfinite(close)
        & (high > 0) & (low > 0) & (close > 0) & (low <= close) & (close <= high)
    )
    tr = np.maximum.reduce([
        high[1:] - low[1:], np.abs(high[1:] - close[:-1]), np.abs(low[1:] - close[:-1]),
    ])
    count = len(close) - 14
    ok = valid[:count].copy()
    total = np.zeros_like(close[14:], dtype=np.float64)
    compensation = np.zeros_like(total)
    for offset in range(1, 15):
        ok &= valid[offset:offset + count]
        link = links[offset:offset + count]
        ok &= link if close.ndim == 1 else link[:, None]
        value = tr[offset - 1:offset - 1 + count]
        added = total + value
        compensation += np.where(
            np.abs(total) >= np.abs(value), (total - added) + value, (value - added) + total,
        )
        total = added
    # The second row's published predecessor proves the first daily identity;
    # a gap before that first row is outside this 15-bar window.
    total += compensation
    ok &= np.isfinite(total)
    np.copyto(atr[14:], total / 14.0, where=ok)
    np.divide(atr, close, out=ratio, where=valid)
    return atr, ratio


def atr14_sma_expr() -> pl.Expr:
    """Group by symbol and chronological daily date, preserving row alignment."""
    def values(series: pl.Series) -> pl.Series:
        frame = series.struct.unnest()
        _, ratio = atr14_sma_arrays(
            frame["high"].to_numpy(), frame["low"].to_numpy(), frame["close"].to_numpy(),
            daily_links(frame["date"].to_list()),
        )
        return pl.Series(ratio, dtype=pl.Float64).fill_nan(None)

    return (
        pl.struct("date", "high", "low", "close")
        .map_batches(values, return_dtype=pl.Float64)
        .over("symbol", order_by="date")
    )


def exact_atr14_sma_reference(
    daily: pl.DataFrame | None, symbols: Sequence[str], expected_dates: Sequence[date],
) -> pl.DataFrame:
    """Use caller's exact published 15-session window; bad candidates abort scan."""
    required = {"symbol", "date", "high", "low", "close", "raw_close"}
    if daily is None or not required <= set(daily.columns):
        raise ValueError("ATR14排名缺少完整日K或未复权昨收")
    if (len(expected_dates) != 15 or len(set(expected_dates)) != 15
            or list(expected_dates) != sorted(expected_dates)):
        raise ValueError("ATR14排名需要精确连续15个交易日")
    window = daily.filter(
        pl.col("date").is_in(expected_dates) & pl.col("symbol").is_in(symbols),
    ).sort("symbol", "date")
    groups = window.partition_by("symbol", as_dict=True)
    result = []
    for symbol in symbols:
        frame = groups.get((symbol,))
        if frame is None or frame["date"].to_list() != list(expected_dates):
            raise ValueError(f"ATR14 {symbol}缺少连续15根日K或存在重复")
        raw = frame["raw_close"].to_numpy()
        atr, ratio = atr14_sma_arrays(
            frame["high"].to_numpy(), frame["low"].to_numpy(), frame["close"].to_numpy(),
            np.ones(15, dtype=bool),
        )
        if not np.isfinite(ratio[-1]) or not (np.isfinite(raw) & (raw > 0)).all():
            raise ValueError(f"ATR14 {symbol}存在无效日K价格")
        result.append({
            "symbol": symbol, "atr14": float(atr[-1]),
            "atr14_previous_close": float(ratio[-1]), "atr_reference_date": expected_dates[-1],
            "atr_raw_previous_close": float(raw[-1]),
        })
    return pl.DataFrame(result)
