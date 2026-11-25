"""ATR14简单均值的共享日线内核, 供因子、矩阵和冻结分钟策略参考复用。"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import polars as pl

FACTOR_ID = "atr14_sma_pct"


def daily_links(days: Sequence[date]) -> np.ndarray:
    """按已发布交易日历确认相邻日; 未知日历或分钟轴不能充当完整日线窗口。"""
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
    """计算一维日线或按日期和股票组织的矩阵ATR及ATR/末日收盘价, 与输入形状对齐。

    links标记当前日与前一日相邻; 15根有效日K形成14个TR, Float64补偿
    求和后取简单均值。预热、缺值、非法OHLC或窗口断链位置保留NaN。
    本内核不复权, 调用方须提供一致价格口径。
    """
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
    # 第二根与首根的相邻关系证明窗口起点; 首根之前的缺口不属于本次15根窗口。
    total += compensation
    ok &= np.isfinite(total)
    np.copyto(atr[14:], total / 14.0, where=ok)
    np.divide(atr, close, out=ratio, where=valid)
    return atr, ratio


def atr14_sma_expr() -> pl.Expr:
    """按股票和日期计算因子, 缺值转null并保持原行对齐。"""
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
    """按调用方给出的15个交易日验证每股窗口, 返回末日ATR及两种昨收参考。

    expected_dates须已按交易日历解析, 本函数检查数量、顺序、唯一性及每股
    实际日期完全匹配。ATR分母为一致复权close, atr_raw_previous_close为
    未复权价格。任一成员缺根、重复或价格异常即抛ValueError, 不返回半份
    批量结果; 预选调用方负责逐股重算、隔离并保留资格。
    """
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
