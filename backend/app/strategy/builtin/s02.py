"""指定池尾盘双周期动量, 扣费净止损6%、毛止盈6%。"""
from app.strategy.builtin._momentum_members import MEMBERS
from app.strategy.momentum_selection import PARAMS as SHARED_PARAMS
from app.strategy.momentum_selection import select_entries

PARAMS = [{**p, "default": "15:00" if p["id"] in {"guard_time", "sell_deadline"}
           else p["default"]} for p in SHARED_PARAMS]

META = {
    "id": "s02", "name": "s02 · 净6% / 毛6%",
    "description": "前收盘5~100元、前交易日上市满60自然日、剔除ST/*ST; 14:30双周期MA5筛选; 14:32最高价、50%分钟容量买入; 次交易日全天净6%止损、毛6%止盈, 15:00兜底及截止。",
    "asset_types": ["stock"], "timeframes": ["1m"], "tags": ["隔夜", "动量", "分钟"],
    "daily_history_bars": 25, "params": PARAMS, "scoring": {},
    "order_by": "return_1430", "descending": True, "limit": 10000,
    "minute_execution": "momentum_capacity_v1",
}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"data_reference": "previous_trade_day", "price_min": 5., "price_max": 100.,
                "market_cap_min": None, "amount_min": None,
                "exclude_st": True, "exclude_new_days": 60}


def filter_minute_history(df, params, *, daily=None):
    return select_entries(df, params, daily=daily, members=MEMBERS)
