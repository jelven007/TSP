"""S02完整分钟示例：预选保存ATR，买入复用前交易日指标。"""
from datetime import date

import polars as pl

from app.strategy.market_data import get_atr14_sma_reference, previous_session

META = {
    "id": "s02_atr14_example",
    "result_sections": "signals",
    "name": "s02 ATR14/昨收示例",
    "description": "尾盘走强，ATR14/昨收降序，次交易日早盘清仓",
    "tags": ["自定义", "分钟", "隔夜"],
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    "daily_history_bars": 21,
    "params": [],
    "scoring": {},
    "scoring_mode": "strategy",
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
当日完整池仅按股票范围与ST/停牌状态形成；流通市值及其他数值只用于策略选股。
入场沿尾盘走强信号，每只股票保留首次真实触发分钟，返回全部合格命中。
TR=max(最高-最低, abs(最高-前收), abs(最低-前收))，使用一致复权日线。
ATR14=截至精确T-1的14个TR简单均值；比率=ATR14/T-1复权收盘，按比率降序。
共享因子atr14_sma_pct；分钟策略通过受信任入口复用同一数值内核。
同值按触发涨幅降序、代码升序；触发涨幅使用分钟价/T-1未复权收盘-1。
ATR在前交易日16:00预选时计算并保存；买入复用已保存指标，不再读取15根日K重算。
缺失ATR仍保留买入信号，排在已知ATR之后，指标和排名分数保持未知。
atr_rank为1起始顺位；score按排名线性映射到0～100分，第一名100、末名0，单只50。
scoring_mode=strategy由源码固定排名，旧涨幅权重和评分方向不能覆盖。
出场沿早盘清仓，与排名无关；资金、最多5仓、T+1和成交约束由交易模块执行。
"""


def filter_minute_history(
    df: pl.DataFrame, params: dict, *, daily: pl.DataFrame | None = None, selection=None,
) -> pl.DataFrame:
    hits = (
        df.filter(pl.col("csgi_late_session_strength").fill_null(False))
        .sort("symbol", "datetime")
        .unique("symbol", keep="first", maintain_order=True)
        .rename({"datetime": "last_datetime"})
    )
    saved = {row["symbol"]: row for row in (selection or {}).get("rows", [])}
    rows = []
    for hit in hits.to_dicts():
        reference = saved.get(hit["symbol"], {})
        ratio = reference.get("atr14_previous_close")
        valid = isinstance(ratio, (int, float)) and 0 <= ratio < float("inf")
        previous = reference.get("reference_close")
        change = (
            hit["close"] / previous - 1
            if isinstance(previous, (int, float)) and 0 < previous < float("inf") else None
        )
        reference_date = reference.get("atr_reference_date") or (selection or {}).get("source_date")
        if isinstance(reference_date, str):
            reference_date = date.fromisoformat(reference_date)
        rows.append({
            **hit, "atr14": reference.get("atr14") if valid else None,
            "atr14_previous_close": ratio if valid else None,
            "atr_reference_date": reference_date,
            "change_pct": change,
            "atr_metric_error": None if valid else
            reference.get("atr_metric_error") or "前日预选ATR缺失, 保留信号并排在末尾",
        })
    ranked = (pl.DataFrame(rows) if rows else hits.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("atr14_previous_close"))).sort(
        ["atr14_previous_close", "change_pct", "symbol"] if rows else ["symbol"],
        descending=[True, True, False] if rows else [False], nulls_last=True)
    count = sum(row["atr14_previous_close"] is not None for row in rows)
    return ranked.with_columns(
        pl.Series("atr_rank", [i + 1 if i < count else None for i in range(ranked.height)], dtype=pl.Int64),
        pl.Series("score", [
            (100. * (count - 1 - i) / (count - 1) if count > 1 else 50.) if i < count else None
            for i in range(ranked.height)
        ], dtype=pl.Float64),
    )


def enrich_selection(rows, params, *, daily, as_of):
    """盘后预选全体的ATR顺位；不替换既有选股评分或次日命中顺位。"""
    if rows.is_empty():
        return rows
    expected = []
    day = as_of
    for _ in range(15):
        day = previous_session(day)
        expected.append(day)
    expected.reverse()
    symbols = rows["symbol"].to_list()
    errors = {}
    try:
        metrics = get_atr14_sma_reference(daily, symbols, expected)
    except ValueError:
        # Optional display metrics must not discard qualified stocks with a bad window.
        groups = (
            daily.filter(pl.col("date").is_in(expected)).partition_by("symbol", as_dict=True)
            if daily is not None and {"symbol", "date"} <= set(daily.columns) else {}
        )
        parts = []
        for symbol in symbols:
            try:
                parts.append(get_atr14_sma_reference(groups.get((symbol,)), [symbol], expected))
            except ValueError as exc:
                errors[symbol] = str(exc)
        metrics = pl.concat(parts) if parts else pl.DataFrame(schema={
            "symbol": pl.String, "atr14": pl.Float64, "atr14_previous_close": pl.Float64,
            "atr_reference_date": pl.Date, "atr_raw_previous_close": pl.Float64,
        })
    ranked = metrics.drop("atr_raw_previous_close").sort(
        ["atr14_previous_close", "symbol"], descending=[True, False])
    ranked = ranked.with_columns(
        pl.Series("atr_rank", list(range(1, ranked.height + 1)), dtype=pl.Int64),
        pl.Series("atr_rank_score", [
            100. * (ranked.height - 1 - i) / (ranked.height - 1) if ranked.height > 1 else 50.
            for i in range(ranked.height)
        ], dtype=pl.Float64),
    )
    return rows.join(ranked, on="symbol", how="left", maintain_order="left").with_columns(
        pl.lit(ranked.height).alias("atr_rank_population"),
        pl.Series("atr_metric_error", [errors.get(symbol) for symbol in symbols], dtype=pl.String),
    )
