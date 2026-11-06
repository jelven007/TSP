"""S02完整分钟示例：按前交易日ATR14简单均值/昨收排名。"""
import numpy as np
import polars as pl

from app.strategy.market_data import previous_session

META = {
    "id": "s02_atr14_example",
    "result_sections": "signals",
    "name": "s02 ATR14/昨收示例",
    "description": "20日均成交额≥1亿元，尾盘走强，ATR14/昨收降序，次交易日早盘清仓",
    "tags": ["自定义", "分钟", "隔夜"],
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    "daily_history_bars": 21,
    "params": [],
    "scoring": {},
    "order_by": "score",
    "descending": True,
    "tie_break": "symbol",
    "limit": 100,
}

BASIC_FILTER = {
    "boards": ["沪主板", "深主板"], "enabled": True,
    "price_max": None, "price_min": None, "amount_max": None, "amount_min": None,
    "exclude_st": True, "target_mode": "selected", "turnover_max": None, "turnover_min": None,
    "float_cap_max": 50000000000, "float_cap_min": 5000000000,
    "change_pct_max": 6, "change_pct_min": 0.5, "data_reference": "previous_trade_day",
    "include_custom": False, "market_cap_max": None, "market_cap_min": None,
    "exclude_new_days": None, "watchlist_group_ids": [], "exclude_suspended": True,
}

EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = ["csgi_late_session_strength"]
EXIT_SIGNALS = ["csgi_morning_liquidation"]
REQUIRED_FEATURES = ["csgi_late_session_strength"]
STOP_LOSS = None
MAX_HOLD_DAYS = None

RULES = """
当日完整池按股票范围、ST/停牌及流通市值形成；其他数值与成交额条件只过滤候选。
入场沿尾盘走强信号，每只股票保留首次真实触发分钟，返回全部合格命中。
TR=max(最高-最低, abs(最高-前收), abs(最低-前收))，使用一致复权日线。
ATR14=截至精确T-1的14个TR简单均值；比率=ATR14/T-1复权收盘，按比率降序。
同值按触发涨幅降序、代码升序；触发涨幅使用分钟价/T-1未复权收盘-1。
需要连续15个已完成交易日日K；缺根、重复、无效价或未知日历阻止本次扫描。
score只编码上述排名；配套覆盖配置必须以scoring_replace清空旧涨幅权重。
出场沿早盘清仓，与排名无关；资金、最多5仓、T+1和成交约束由交易模块执行。
"""


def filter_minute_history(
    df: pl.DataFrame, params: dict, *, daily: pl.DataFrame | None = None,
) -> pl.DataFrame:
    hits = (
        df.filter(pl.col("csgi_late_session_strength").fill_null(False))
        .sort("symbol", "datetime")
        .unique("symbol", keep="first", maintain_order=True)
        .rename({"datetime": "last_datetime"})
    )
    if hits.is_empty():
        return hits
    days = hits["last_datetime"].dt.date().unique().to_list()
    if len(days) != 1:
        raise ValueError("ATR14排名需要单一交易日的分钟窗口")
    expected = []
    day = days[0]
    for _ in range(15):
        day = previous_session(day)
        expected.append(day)
    expected.reverse()
    required = {"symbol", "date", "high", "low", "close", "raw_close"}
    if daily is None or not required <= set(daily.columns):
        raise ValueError("ATR14排名缺少完整日K或未复权昨收")
    window = daily.filter(pl.col("date").is_in(expected)).sort("symbol", "date")
    values = []
    for hit in hits.to_dicts():
        symbol = hit["symbol"]
        rows = window.filter(pl.col("symbol") == symbol).to_dicts()
        if [row["date"] for row in rows] != expected:
            raise ValueError(f"ATR14 {symbol}缺少连续15根日K或存在重复")
        for row in rows:
            prices = [row[key] for key in ("high", "low", "close", "raw_close")]
            if (any(value is None or not np.isfinite(value) or value <= 0 for value in prices)
                    or not row["low"] <= row["close"] <= row["high"]):
                raise ValueError(f"ATR14 {symbol}存在无效日K价格")
        tr = [
            max(row["high"] - row["low"], abs(row["high"] - before["close"]),
                abs(row["low"] - before["close"]))
            for before, row in zip(rows, rows[1:], strict=False)
        ]
        atr = sum(tr) / 14
        price = hit["close"]
        if price is None or not np.isfinite(price) or price <= 0:
            raise ValueError(f"ATR14 {symbol}存在无效触发价")
        values.append({
            "symbol": symbol, "atr14": atr, "atr14_previous_close": atr / rows[-1]["close"],
            "atr_reference_date": expected[-1],
            "change_pct": price / rows[-1]["raw_close"] - 1,
        })
    metrics = pl.DataFrame(values)
    hits = hits.drop([key for key in metrics.columns if key != "symbol" and key in hits.columns])
    ranked = hits.join(metrics, on="symbol").sort(
        ["atr14_previous_close", "change_pct", "symbol"], descending=[True, True, False],
    )
    return ranked.with_columns(
        pl.Series("score", [float(ranked.height - i) for i in range(ranked.height)]),
    )
