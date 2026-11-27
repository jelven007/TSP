"""S01 测试载体: 内置模板 ``s01`` 已退役并删除源码, 本文件原样保留其定义,
供共享 ``_overnight`` 内核、通用信号及回测回归测试加载; 不是产品内置策略。
只能通过 ``test_s01_strategy.s01_engine()`` 加载, 见该函数说明。"""
from app.strategy.builtin._overnight import PARAMS, select_entries

META = {
    "id": "s01", "name": "s01 · 尾盘低振幅隔夜",
    "description": "14:45完整分钟筛选, 次日11:15早盘均价/盈利回落、短时放量下跌及11:22清仓信号。分钟容量分片; 研究参数尚无独立样本验证。",
    "asset_types": ["stock"], "timeframes": ["1m"], "tags": ["隔夜", "低振幅", "分钟"],
    "daily_history_bars": 2, "params": PARAMS, "scoring": {},
    "order_by": "score", "descending": True, "tie_break": "symbol", "limit": 10000,
    "minute_execution": "overnight_capacity_v1",
}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"price_min": None, "price_max": None, "market_cap_min": None,
                "amount_min": None, "exclude_st": True, "exclude_new_days": 0}


def filter_minute_history(df, params, *, daily=None):
    return select_entries(df, params, daily)
