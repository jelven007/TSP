"""Registered, opt-in entry hypotheses. Original exits and defaults are preserved.

Indicators use valid security bars; observation expiry uses market sessions.
All references are prices in the same adjusted series as the signal matrix.
"""

from typing import NamedTuple

import numpy as np

from app.backtest.matrix import (
    MarketDataMatrix,
    SignalMatrix,
    make_signal_matrix,
    matrix_feature,
    valid_shift,
)

ENTRY_RULE_VERSION = "quality_entry_hypotheses_v1"


class EntryVariant(NamedTuple):
    id: str
    label: str
    fields: tuple[str, ...] = ()
    extra_bars: int = 1


# Each is a single registered topic. No automatic combinations or promotion.
VARIANTS = {
    "ma_golden_cross": (
        EntryVariant("ma20_atr_15", "距MA20不超过1.5ATR", ("atr_14",)),
        EntryVariant("ma20_atr_20", "距MA20不超过2ATR", ("atr_14",)),
    ),
    "macd_golden": (
        EntryVariant("dif_nonnegative", "DIF零轴上分层"),
        EntryVariant("dif_negative", "DIF零轴下分层"),
    ),
    "bullish_alignment": (EntryVariant("first_alignment", "首次形成均线排列"),),
    "volume_price_surge": (
        EntryVariant("volume_cap_4", "量比上限4"),
        EntryVariant("clv_070", "收盘位置至少70%", ("high", "low")),
    ),
    "boll_breakout": (EntryVariant("first_upper_cross", "首次越过布林上轨"),),
    "breakout_new_high_60d": (
        EntryVariant("breakout_atr_1", "突破幅度不超过1ATR", ("atr_14",)),
        EntryVariant("first_high_break", "首次突破60日最高收盘"),
    ),
    "platform_consolidation_breakout": (
        EntryVariant("prior_platform_mean", "平台振幅以平台均价计算"),
        EntryVariant("platform_volume_contract", "平台后半段缩量"),
    ),
    "ma_convergence_breakout": (
        EntryVariant("first_squeeze_release", "带内粘合后首次脱离", extra_bars=16),
        EntryVariant("clv_070", "收盘位置至少70%", ("high", "low")),
    ),
    "pullback_to_support": (
        EntryVariant("confirm_high_3", "回踩后3日内突破形态高点", ("high", "low"), 4),
    ),
    "pullback_ma20_bounce": (
        EntryVariant("first_pullback_10", "突破MA20后10日内首次回踩", extra_bars=11),
        EntryVariant("ma20_nondeclining", "MA20较5日前不下降", extra_bars=5),
    ),
    "rsi_midline_pullback": (
        EntryVariant("rsi_turn_up", "RSI当日拐头上升"),
        EntryVariant("rsi_cross_50", "RSI向上穿越50"),
    ),
    "n_day_low_reversal": (
        EntryVariant("clv_065", "收盘位置至少65%", ("high", "low")),
        EntryVariant("confirm_high_3", "新低形态后3日内突破高点", ("high", "low"), 4),
    ),
    "oversold_bounce": (
        EntryVariant("rsi_recover", "过去3日超卖后上穿RSI阈值", extra_bars=3),
    ),
    "oversold_reversal": (
        EntryVariant("recent_oversold_3", "过去3日超卖后确认", extra_bars=3),
        EntryVariant("volume_cap_4", "量比上限4", ("volume",)),
    ),
    "long_lower_shadow_reversal": (
        EntryVariant("clv_065", "收盘位置至少65%"),
        EntryVariant("confirm_high_1", "次日突破影线日高点", extra_bars=2),
    ),
    "macd_below_zero_revival": (
        EntryVariant("dif_rising_2", "DIF连续两日回升", extra_bars=2),
    ),
    "high_turnover_surge": (EntryVariant("turnover_cap_15", "换手率不超过15%"),),
    "near_limit_up": (
        EntryVariant("not_closed_limit", "收盘未涨停分层", ("raw_close",)),
        EntryVariant("closed_limit", "收盘已涨停分层", ("raw_close",)),
    ),
    "active_limit_gene": (
        EntryVariant("recent_limit_20", "20个市场交易日内有涨停且今日未涨停",
                     ("raw_close",), 20),
    ),
}


def _observed(market: MarketDataMatrix) -> np.ndarray:
    valid = market.tradable.astype(bool) & np.isfinite(market.volume) & (market.volume > 0)
    for prices in (market.open, market.high, market.low, market.close):
        valid &= np.isfinite(prices) & (prices > 0)
    return valid


def first_event(state: np.ndarray, observed: np.ndarray) -> np.ndarray:
    """A missing/suspended bar neither triggers nor resets a continuous state."""
    out = np.zeros(state.shape, dtype=bool)
    previous = np.zeros(state.shape[1], dtype=bool)
    seen = np.zeros(state.shape[1], dtype=bool)
    for t in range(len(state)):
        out[t] = observed[t] & seen & state[t] & ~previous
        previous = np.where(observed[t], state[t], previous)
        seen |= observed[t]
    return out


def confirm_frozen_high(
    market: MarketDataMatrix, seed: np.ndarray, days: int,
) -> tuple[np.ndarray, np.ndarray]:
    """One observation per seed episode, no renewal while watching.

    Low breach has priority over high confirmation on the same daily bar.
    Missing bars consume expiry time; they cannot establish/confirm a pattern.
    The second output is the shape-day row, -1 when no confirmation exists.
    """
    observed = _observed(market)
    starts = first_event(seed, observed)
    entry = np.zeros(market.shape, dtype=bool)
    shape_day = np.full(market.shape, -1, dtype=np.int32)
    started = np.full(market.shape[1], -1, dtype=np.int32)
    high = np.full(market.shape[1], np.nan)
    low = high.copy()
    for t in range(market.shape[0]):
        age = t - started
        active = started >= 0
        expired = active & (age > days)
        broken = active & observed[t] & (market.low[t] < low)
        cancelled = expired | broken
        started[cancelled] = -1
        confirmed = (
            (started >= 0) & (age >= 1) & observed[t]
            & (market.close[t] > high)
        )
        entry[t] = confirmed
        shape_day[t, confirmed] = started[confirmed]
        started[confirmed] = -1
        # A cancelled/confirmed episode cannot silently replace its reference today.
        begin = starts[t] & (started < 0) & ~cancelled & ~confirmed
        begin &= np.isfinite(market.high[t]) & np.isfinite(market.low[t])
        begin &= market.high[t] > market.low[t]
        started[begin] = t
        high[begin] = market.high[t, begin]
        low[begin] = market.low[t, begin]
    return entry, shape_day


def _prior_stack(values: np.ndarray, days: int) -> list[np.ndarray]:
    return [valid_shift(values, k) for k in range(1, days + 1)]


def _first_pullback(market, entry):
    ma20 = matrix_feature(market, "ma20")
    crossing = (market.close > ma20) & (valid_shift(market.close, 1) <= valid_shift(ma20, 1))
    observed = _observed(market)
    starts = np.full(market.shape[1], -1, dtype=np.int32)
    out = np.zeros(market.shape, dtype=bool)
    for t in range(market.shape[0]):
        starts[(t - starts > 10) | (observed[t] & (market.close[t] < ma20[t]))] = -1
        out[t] = observed[t] & entry[t] & (starts >= 0) & (t > starts)
        starts[out[t]] = -1
        starts[crossing[t] & observed[t]] = t
    return out


def _first_squeeze(market, params, entry):
    ma5, ma10, ma20 = (matrix_feature(market, name) for name in ("ma5", "ma10", "ma20"))
    top = np.maximum(np.maximum(ma5, ma10), ma20)
    bottom = np.minimum(np.minimum(ma5, ma10), ma20)
    tight = ((top - bottom) / ma20 * 100) <= float(params.get("spread_pct_max", 2.5))
    observed = _observed(market)
    days = max(2, int(params.get("squeeze_days", 5)))
    count = np.zeros(market.shape[1], dtype=np.int32)
    out = np.zeros(market.shape, dtype=bool)
    for t in range(market.shape[0]):
        out[t] = observed[t] & entry[t] & (count >= days)
        # Count a new consolidation only while price is back inside the band.
        # Departure consumes the episode even if volume confirmation fails.
        inside = tight[t] & (market.close[t] >= bottom[t]) & (market.close[t] <= top[t])
        count = np.where(observed[t], np.where(inside, count + 1, 0), count)
    return out


def _entry_for_variant(base, market, params, variant, signals):
    entry = signals.entry.astype(bool)
    key = variant.id
    if key.startswith("clv_"):
        span = market.high - market.low
        level = market.low + np.float32(int(key[-3:]) / 100) * span
        # Compare in price units to avoid cancellation in float32 CLV division.
        return entry & (span > 0) & (market.close >= level)
    if key.startswith("confirm_high_"):
        return confirm_frozen_high(market, entry, int(key[-1]))[0]
    if key.startswith("ma20_atr_"):
        atr = matrix_feature(market, "atr_14")
        cap = 1.5 if key.endswith("15") else 2.0
        return entry & (atr > 0) & ((market.close - matrix_feature(market, "ma20")) <= cap * atr)
    if key in {"dif_nonnegative", "dif_negative", "dif_rising_2"}:
        dif = matrix_feature(market, "macd_dif")
        if key == "dif_rising_2":
            return entry & (dif > valid_shift(dif, 1)) & (valid_shift(dif, 1) > valid_shift(dif, 2))
        return entry & ((dif >= 0) if key == "dif_nonnegative" else (dif < 0))
    if key == "volume_cap_4":
        return entry & (matrix_feature(market, "vol_ratio_5d") <= 4)
    if key == "turnover_cap_15":
        return entry & (matrix_feature(market, "turnover_rate") <= 15)
    if key == "ma20_nondeclining":
        ma20 = matrix_feature(market, "ma20")
        return entry & (ma20 >= valid_shift(ma20, 5))
    if key == "first_alignment":
        ma5, ma10, ma20, ma60 = (
            matrix_feature(market, name) for name in ("ma5", "ma10", "ma20", "ma60")
        )
        return entry & first_event((ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60), _observed(market))
    if key == "first_upper_cross":
        upper = matrix_feature(market, "boll_upper")
        return entry & (valid_shift(market.close, 1) <= valid_shift(upper, 1))
    if key in {"breakout_atr_1", "first_high_break"}:
        level = valid_shift(matrix_feature(market, "high_60d"), 1)
        if key == "breakout_atr_1":
            atr = matrix_feature(market, "atr_14")
            return entry & (atr > 0) & (market.close - level <= atr)
        return entry & first_event(market.close > level, _observed(market))
    if key in {"prior_platform_mean", "platform_volume_contract"}:
        days = max(4, int(params.get("platform_days", 10)))
        # Advance every column on the SAME complete security bars. Independently
        # skipping NaNs can mix different dates for H/L/C/volume in one platform.
        observed = _observed(market)
        highs, lows, closes, volumes = (
            np.where(observed, values, np.nan)
            for values in (market.high, market.low, market.close, market.volume)
        )
        top = valid_shift(highs, 1).copy()
        bottom = valid_shift(lows, 1).copy()
        mean = valid_shift(closes, 1).copy()
        for k in range(2, days + 1):
            np.maximum(top, valid_shift(highs, k), out=top)
            np.minimum(bottom, valid_shift(lows, k), out=bottom)
            mean += valid_shift(closes, k)
        mean /= days
        denominator = mean if key == "prior_platform_mean" else market.close
        candidate = (denominator > 0) & (
            (top - bottom) / denominator * 100 <= float(params.get("range_pct_max", 8))
        )
        candidate &= (market.close > top) & (
            matrix_feature(market, "vol_ratio_5d") >= float(params.get("vol_ratio_min", 1.5))
        )
        if key == "platform_volume_contract":
            # Odd windows exclude the middle bar, keeping equal-length halves.
            half = days // 2
            recent = np.zeros(market.shape, dtype=np.float32)
            older = recent.copy()
            for k in range(1, half + 1):
                recent += valid_shift(volumes, k)
                older += valid_shift(volumes, days - k + 1)
            candidate &= (recent < older) & (recent > 0) & (older > 0)
        return candidate
    if key == "first_squeeze_release":
        return _first_squeeze(market, params, entry)
    if key == "first_pullback_10":
        return _first_pullback(market, entry)
    if key in {"rsi_turn_up", "rsi_cross_50"}:
        rsi = matrix_feature(market, f"rsi_{int(params.get('rsi_period', 14))}")
        previous = valid_shift(rsi, 1)
        condition = rsi > previous if key == "rsi_turn_up" else (rsi > 50) & (previous <= 50)
        return entry & condition
    if key in {"rsi_recover", "recent_oversold_3"}:
        # Replace the same-day oversold requirement. Retain all other user parameters.
        entry = base.compute_signals(market, {**params, "use_rsi_filter": False}).entry.astype(bool)
        rsi = matrix_feature(market, "rsi_14")
        threshold = float(params.get("rsi_max", 30))
        previous = _prior_stack(rsi, 3)
        recent = np.logical_or.reduce([item < threshold for item in previous])
        entry &= recent & np.isfinite(rsi)
        if key == "rsi_recover":
            entry &= (rsi >= threshold) & (previous[0] < threshold)
        return entry
    if key in {"not_closed_limit", "closed_limit", "recent_limit_20"}:
        locked = market.limit_up_locked.astype(bool)
        if key == "recent_limit_20":
            recent = np.zeros(market.shape, dtype=bool)
            for k in range(1, 21):
                recent[k:] |= locked[:-k]
            return entry & recent & ~locked
        return entry & (locked if key == "closed_limit" else ~locked)
    raise ValueError(f"unsupported entry variant: {key}")


class QualityEntryStrategy:
    def __init__(self, base, variants):
        self.base = base
        self.variants = {item.id: item for item in variants}

    def _variant(self, params):
        key = params.get("entry_variant", "original")
        if key == "original":
            return None
        if key not in self.variants:
            raise ValueError(f"unknown entry_variant: {key}")
        return self.variants[key]

    def required_fields(self):
        return self.base.required_fields()

    def required_fields_for_params(self, params):
        variant = self._variant(params)
        return frozenset(variant.fields if variant else ())

    def required_warmup_bars(self, params):
        variant = self._variant(params)
        return self.base.required_warmup_bars(params) + (variant.extra_bars if variant else 0)

    def compute_signals(self, market, params) -> SignalMatrix:
        variant = self._variant(params)
        signals = self.base.compute_signals(market, params)
        if variant is None:
            return signals
        entry = _entry_for_variant(self.base, market, params, variant, signals)
        entry &= _observed(market)
        return make_signal_matrix(
            market.shape, entry=entry.astype(np.uint8), exit=signals.exit, score=signals.score,
            entry_signal_code=np.where(entry, 0, -1).astype(np.int16),
            entry_signal_ids=(f"quality_{variant.id}",),
            exit_signal_code=signals.exit_signal_code,
            exit_signal_ids=signals.exit_signal_ids,
        )


def with_quality_entries(meta, base):
    variants = VARIANTS[meta["id"]]
    meta["params"].append({
        "id": "entry_variant", "label": "入场规则 (候选需验证)",
        "type": "select", "default": "original",
        "options": ["original", *(item.id for item in variants)],
        "option_labels": {"original": "原版", **{item.id: item.label for item in variants}},
    })
    meta["entry_rule_version"] = ENTRY_RULE_VERSION
    return QualityEntryStrategy(base, variants)
