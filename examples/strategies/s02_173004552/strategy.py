"""173004552 三分支策略, 使用独立身份。"""

import polars as pl

META = {
    "id": "custom_mvxxcs05", "name": "173004552 三分支", "result_sections": "signals",
    "description": "173004552: 四维前50, 按前日市场状态选择一个分钟入场分支",
    "asset_types": ["stock"], "timeframes": ["1m"], "tags": ["自定义", "分钟", "隔夜"],
    "daily_history_bars": 70, "params": [], "scoring": {}, "scoring_mode": "strategy",
    "entry_routing": "s02_173004552_v1", "entry_signal_match": "any",
    "entry_prior_ex_date_exclusion": True, "order_by": "score",
    "descending": True, "tie_break": "symbol", "limit": 100,
}
BASIC_FILTER = {
    "boards": ["沪主板", "深主板"], "enabled": True, "exclude_st": True,
    "exclude_suspended": True, "target_mode": "selected", "data_reference": "previous_trade_day",
    "price_min": 4, "price_max": 150, "turnover_min": 1, "turnover_max": 11,
    "float_cap_min": 5000000000, "float_cap_max": 50000000000,
    "change_pct_min": .5, "change_pct_max": 6, "watchlist_group_ids": [],
}
EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = [
    "csgi_tail_entry_s05", "csgi_low_gap_entry_s05",
    "csgi_board_entry_s05",
]
EXIT_SIGNALS = [
    "csgi_tail_exit_s05", "csgi_low_gap_exit_s05",
    "csgi_board_exit_s05",
]
STOP_LOSS = None
MAX_HOLD_DAYS = None
RULES = "分流、入场和分支退出契约见 docs/reference/strategy-173004552.md。"


def filter_minute_history(df, params, *, daily=None, selection=None):
    """Project the single independently evaluated entry point."""
    columns = [name for name in ENTRY_SIGNALS if name in df.columns]
    if not columns:
        return df.head(0).rename({"datetime": "last_datetime"}).with_columns(
            pl.lit(None, dtype=pl.Float64).alias("score"))
    return (df.filter(pl.any_horizontal(pl.col(col).fill_null(False) for col in columns))
            .sort("datetime").unique("symbol", keep="first")
            .rename({"datetime": "last_datetime"}))
