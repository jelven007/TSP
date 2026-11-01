"""指定池尾盘双周期动量, 扣费净止损6%、毛止盈6%。"""
from app.strategy.builtin._momentum_members import MEMBERS
from app.strategy.momentum_selection import PARAMS, select_entries

META = {
    "id": "s02", "name": "s02 · 净6% / 毛6%",
    "description": "14:30双周期MA5筛选; 14:32最高价、50%分钟容量买入; 次交易日净6%止损、毛6%止盈, 10:30兜底, 11:30截止。",
    "asset_types": ["stock"], "timeframes": ["1m"], "tags": ["隔夜", "动量", "分钟"],
    "daily_history_bars": 25, "params": PARAMS, "scoring": {},
    "order_by": "return_1430", "descending": True, "limit": 10000,
    "minute_execution": "momentum_capacity_v1",
}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"price_min": None, "price_max": None, "market_cap_min": None,
                "amount_min": None, "exclude_st": False, "exclude_new_days": 0}


def filter_minute_history(df, params, *, daily=None):
    return select_entries(df, params, daily=daily, members=MEMBERS)
