"""S03：S02冻结候选的五维前30预选、固定时点A/B建仓及早盘清仓。

当前有效规则的唯一安装源；完整规则及异常契约见docs/reference/s03.md。
盘后服务冻结五维前30，信号内核完成A/B门控与出场。本文件不编排定时任务、
写归档或模拟成交；修改此文件后仍需通过受控保存入口才能更新运行定义。
"""
# ruff: noqa: RUF001, RUF002, RUF003 -- Chinese strategy rules and documentation punctuation.
import polars as pl

META = {
    "id": "custom_mvxxcs03", "result_sections": "signals", "name": "s03",
    "description": "s02升级版", "tags": ["自定义", "分钟", "隔夜"],
    "asset_types": ["stock"], "timeframes": ["1m"],
    # 与保存定义保持相同预热窗口；五维评分另读65条实际完成日线。
    "daily_history_bars": 70, "params": [], "scoring": {}, "scoring_mode": "strategy",
    "selection_ordering": "s03_ab_fund_v1",
    "selection_projection": "s03_five_dimension_rows_v1",
    "order_by": "score", "descending": True, "tie_break": "symbol", "limit": 100,
}

# 完整池只使用策略自己的范围及ST/停牌选择；不再叠加S02的数值条件。
BASIC_FILTER = {
    "boards": ["沪主板", "深主板"], "enabled": True,
    "price_max": None, "price_min": None, "amount_max": None, "amount_min": None,
    "exclude_st": True, "target_mode": "selected", "turnover_max": None, "turnover_min": None,
    "float_cap_max": None, "float_cap_min": None,
    "change_pct_max": None, "change_pct_min": None, "data_reference": "previous_trade_day",
    "include_custom": False, "market_cap_max": None, "market_cap_min": None,
    "exclude_new_days": None, "watchlist_group_ids": [], "exclude_suspended": True,
}
EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = ["csgi_late_session_strength_s03"]
EXIT_SIGNALS = ["csgi_morning_liquidation_s03"]
REQUIRED_FEATURES = ["csgi_late_session_strength_s03"]
STOP_LOSS = None
MAX_HOLD_DAYS = None

RULES = """
22:00～23:00消费同用户S02在21:00～22:00成功冻结的精确下一交易日候选。
技术40%、资金20%、情绪20%、基本面10%、消息10%评分，综合分降序取前30，同分代码升序。
买入使用已保存早盘建仓信号：10:00启动分钟，10:00:30确认，10:30补数重试及提交截止。
共用条件AND后，A组AND或B组AND；双命中归A。成功零命中固定，不逐分钟重选。
逐股判断A/B后，A组按五维排名升序，B组按资金分降序、五维排名升序、代码升序。
卖出从09:54开始，成本止盈3%或止损6%，14:30时钟清仓，14:58停止卖出。
具体时钟及阈值由当前已保存信号决定，源码默认不覆盖用户后续设置。
单股数据异常隔离并留原因，有候选但全无可算成员失败；正常空候选可成功归档。
"""


def filter_minute_history(
    df: pl.DataFrame, params: dict, *, daily: pl.DataFrame | None = None, selection=None,
) -> pl.DataFrame:
    """投影每股首次真实命中与前日冻结值，不读取目标日后续分钟或重算选股。

    daily由统一执行接口提供，本投影不消费；入场时间与A/B优先序已由信号
    内核确定。最终score由引擎按entry_priority映射，
    这里的冻结综合分另存为strategy_score。
    """
    hits = (
        df.filter(pl.col("csgi_late_session_strength_s03").fill_null(False))
        .sort("symbol", "datetime").unique("symbol", keep="first", maintain_order=True)
        .rename({"datetime": "last_datetime"})
    )
    saved = {row["symbol"]: row for row in (selection or {}).get("rows", [])}
    rows = []
    for hit in hits.to_dicts():
        reference = saved.get(hit["symbol"], {})
        # reference_close为归档原始昨收，分钟close为原价，涨幅输出小数。
        previous = reference.get("reference_close")
        change = (hit["close"] / previous - 1
                  if isinstance(previous, (int, float)) and 0 < previous < float("inf") else None)
        rows.append({
            **hit, "change_pct": change,
            # 五维分用于诊断与A/B条件，最终执行排序沿共享内核的入场优先序。
            "score": reference.get("score"),
        })
    return (pl.DataFrame(rows, infer_schema_length=None) if rows else hits.with_columns(
        pl.lit(None, dtype=pl.Float64).alias("score")))
