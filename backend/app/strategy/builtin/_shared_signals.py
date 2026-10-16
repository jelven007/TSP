"""Shared calculations; each strategy retains its own confirmation and exits."""
import numpy as np

from app.backtest.matrix import MarketDataMatrix, matrix_feature, valid_shift


def rsi_oversold_entry(market: MarketDataMatrix, params: dict) -> np.ndarray:
    if params.get("use_rsi_filter", True):
        return matrix_feature(market, "rsi_14") < float(params.get("rsi_max", 30.0))
    return np.ones(market.shape, dtype=bool)


def ma20_proximity(market: MarketDataMatrix, ma20: np.ndarray, fraction: float) -> np.ndarray:
    """The caller converts its saved parameter unit to a fraction explicitly."""
    return (market.close > ma20 * (1.0 - fraction)) & (
        market.close < ma20 * (1.0 + fraction)
    )


def ma20_breakdown(market: MarketDataMatrix, ma20: np.ndarray) -> np.ndarray:
    return (market.close < ma20) & (
        valid_shift(market.close, 1) >= valid_shift(ma20, 1)
    )
