"""Trading intent groups, not independent sources of portfolio diversification."""

BUILTIN_FAMILIES = {
    "trend": ("趋势跟随", (
        "ma_golden_cross", "macd_golden", "bullish_alignment", "low_volatility_leader",
    )),
    "breakout": ("放量突破", (
        "volume_price_surge", "boll_breakout", "breakout_new_high_60d",
        "platform_consolidation_breakout", "ma_convergence_breakout",
    )),
    "pullback": ("趋势回踩", (
        "pullback_to_support", "pullback_ma20_bounce", "rsi_midline_pullback",
    )),
    "reversal": ("超跌修复", (
        "n_day_low_reversal", "oversold_bounce", "oversold_reversal",
        "long_lower_shadow_reversal", "macd_below_zero_revival",
    )),
    "momentum": ("强势延续", (
        "high_turnover_surge", "strong_open", "near_limit_up",
    )),
    "limit": ("涨停生态", ("active_limit_gene", "consecutive_limit_ups")),
}


def builtin_family(strategy_id: str) -> dict[str, str] | None:
    for family_id, (name, strategy_ids) in BUILTIN_FAMILIES.items():
        if strategy_id in strategy_ids:
            return {"id": family_id, "name": name}
    return None
