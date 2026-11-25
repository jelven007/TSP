"""S02策略源码入口: 预选补充ATR14, 买名单复用首次信号与冻结顺位。

稳定ID为custom_mvxxcp88; 当前保存覆盖、信号参数及完整规则见
docs/reference/s02.md。本文件不负责采集、任务重试、阶段汇合或成交。
"""
from datetime import date

import polars as pl

from app.strategy.market_data import get_atr14_sma_reference, previous_session

# 当前定义及调用链见 docs/reference/s02.md。
# 配套 overrides.json 保存运行覆盖, signals.json 保存入场/出场信号参数;
# 引擎先合并覆盖并注入信号列, 本文件只补充预选ATR和排列已触发的买名单。
META = {
    # 稳定ID用于关联配置、每日归档和次日卖名单; 显示名称可独立修改。
    "id": "custom_mvxxcp88",
    # 使用卖/选/买信号分区; 名单表示策略信号, 不能当作实际成交。
    "result_sections": "signals",
    # 声明两侧独立取数、计算和冻结; 是否要求广度通过由保存信号的下限决定。
    # Full由PhaseInbox非阻塞收取回执, Worker提交后确认; 声明本身不执行发布。
    "entry_phase_policy": "independent_v1",
    # 下限为空时仍观察真实广度; 观测异常不改变个股买任务的终态或已发布名单。
    "entry_breadth_observation": True,
    "name": "s02",
    "description": "尾盘走强，ATR14/昨收降序，次交易日早盘清仓",
    "tags": ["自定义", "分钟", "隔夜"],
    "asset_types": ["stock"],
    "timeframes": ["1m"],
    # 声明预选日线依赖; ATR实际使用精确15个交易日, 盘中排名只读取冻结快照。
    "daily_history_bars": 21,
    # 没有独立策略参数: 入场和出场阈值由所关联的自定义信号维护。
    "params": [],
    "scoring": {},
    # 排名分由本文件产生, 通用因子评分不能替换这个确定性顺序。
    "scoring_mode": "strategy",
    "order_by": "score",
    "descending": True,
    "tie_break": "symbol",
    # 通用目录元数据; 信号分区的完整资格和买名单不按此值裁剪。
    "limit": 100,
    # 预选归属下一交易日; 此处“前一日”就是正在计算的源交易日。
    "entry_prior_ex_date_exclusion": True,
}

# 源默认过滤与保存覆盖是两个层次, null表示该数值项在本层不限制。
# 价格和换手率由配套覆盖设置; 下面的源默认值保持为空, 不混同为运行保存值。
# boards/分组及ST/停牌决定完整范围; 数值条件只决定预选资格, 不缩小完整池。
BASIC_FILTER = {
    "boards": ["沪主板", "深主板"], "enabled": True,
    "price_max": None, "price_min": None, "amount_max": None, "amount_min": None,
    "exclude_st": True, "target_mode": "selected", "turnover_max": None, "turnover_min": None,
    "float_cap_max": 50000000000, "float_cap_min": 5000000000,  # 元, 分别为500/50亿元。
    # 涨幅配置是百分数值; previous_trade_day指定精确前交易日已完成日线。
    "change_pct_max": 6, "change_pct_min": 0.5, "data_reference": "previous_trade_day",
    "include_custom": False, "market_cap_max": None, "market_cap_min": None,
    "exclude_new_days": None, "watchlist_group_ids": [], "exclude_suspended": True,
}

# minute_filter由引擎提供完成分钟; csgi_列对应配套JSON中的信号稳定ID。
# 入场/出场的时钟和阈值由配套保存信号维护, 不在此形成第二套计算规则。
EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = ["csgi_late_session_strength"]
EXIT_SIGNALS = ["csgi_morning_liquidation"]
REQUIRED_FEATURES = ["csgi_late_session_strength"]
# 不额外设置策略级止损或最长持有; 出场沿保存的早盘清仓信号执行。
STOP_LOSS = None
MAX_HOLD_DAYS = None

# 保留源码展示字段, 规则正文只在一处维护; 不复制会与实际输入门禁脱节的旧摘要。
RULES = "当前完整规则、保存参数、输入门禁及调用链统一见 docs/reference/s02.md。"


def filter_minute_history(
    df: pl.DataFrame, params: dict, *, daily: pl.DataFrame | None = None, selection=None,
) -> pl.DataFrame:
    """从引擎注入的分钟真值取每股首次入场, 复用冻结预选参考。

    selection.rows提供原始昨收、ATR比率、顺位和排名分; daily/params保留
    策略函数接口。本函数不重算资格或ATR, 缺指标仍保留真实信号并排末尾。
    返回的change_pct为小数, atr_rank保留原空档; 最终发布由任务汇合负责。
    """
    # 共享信号内核负责资格和MA门槛; 只取首次命中, 避免后续分钟改变触发价。
    hits = (
        df.filter(pl.col("csgi_late_session_strength").fill_null(False))
        .sort("symbol", "datetime")
        .unique("symbol", keep="first", maintain_order=True)
        .rename({"datetime": "last_datetime"})
    )
    # selection是目标交易日已冻结的前日预选; daily保留接口契约, 买入不使用它重算ATR。
    saved = {row["symbol"]: row for row in (selection or {}).get("rows", [])}
    rows = []
    for hit in hits.to_dicts():
        reference = saved.get(hit["symbol"], {})
        ratio = reference.get("atr14_previous_close")
        # ATR比率是小数制, 例如0.02表示2%; 未知/非有限值保留命中, 仅不参与已知排名。
        valid = isinstance(ratio, (int, float)) and 0 <= ratio < float("inf")
        previous = reference.get("reference_close")
        # 展示触发涨幅; 分母为冻结的未复权昨收, 不用当天涨幅改写预选排名。
        change = (hit["close"] / previous - 1
                  if isinstance(previous, (int, float)) and 0 < previous < float("inf") else None)
        reference_date = reference.get("atr_reference_date") or (selection or {}).get("source_date")
        # 持久化JSON恢复为字符串, 统一转date, 避免混合类型或长名单推断产生错误。
        if isinstance(reference_date, str):
            reference_date = date.fromisoformat(reference_date)
        rank = reference.get("atr_rank")
        rank = rank if valid and isinstance(rank, int) and not isinstance(rank, bool) and rank > 0 else None
        score = reference.get("atr_rank_score")
        score = score if rank is not None and isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= 100 else None
        rows.append({**hit, "atr14": reference.get("atr14") if valid else None,
                     "atr14_previous_close": ratio if valid else None,
                     "atr_reference_date": reference_date,
                     "atr_rank": rank, "atr_rank_score": score, "score": score,
                     "atr_rank_population": reference.get("atr_rank_population"),
                     "change_pct": change,
                     "atr_metric_error": None if valid else
                     reference.get("atr_metric_error") or "前日预选ATR缺失, 保留信号并排在末尾"})
    # 全量推断覆盖长名单稀疏字段; 没保存排名则保持未知, 不在买入日补算或压缩顺位。
    if not rows:
        return hits.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("atr_rank"),
            pl.lit(None, dtype=pl.Float64).alias("score"))
    return pl.DataFrame(rows, infer_schema_length=None).sort(
        ["atr_rank", "symbol"], nulls_last=True)


def enrich_selection(rows, params, *, daily, as_of):
    """为目标交易日as_of的资格行追加ATR, 不增删成员或替换已有score。

    daily须含一致复权OHLC及raw_close; 日历解析失败向上抛出, ATR窗口或
    价格校验的ValueError逐股隔离并写atr_metric_error。已知比率按降序、
    同值按代码升序编号; 未知指标不参与排名总数, 单个有效成员评分为50。
    """
    if rows.is_empty():
        return rows
    # as_of是资格归属的目标交易日; 从它向前按已发布日历取15个完成交易日。
    # 15根日K提供14个TR, 最后一根必须是精确T-1; 不能用自然日或最近有行情日替代。
    expected = []
    day = as_of
    for _ in range(15):
        day = previous_session(day)
        expected.append(day)
    expected.reverse()
    symbols = rows["symbol"].to_list()
    errors = {}
    try:
        # 共享ATR内核一次批算; 使用一致复权high/low/close计算简单均值及相对波动。
        metrics = get_atr14_sma_reference(daily, symbols, expected)
    except ValueError:
        # 批量窗口校验失败后逐股隔离, 避免一股缺日K/重复/价格异常删除其他资格。
        groups = daily.filter(pl.col("date").is_in(expected)).partition_by("symbol", as_dict=True) \
            if daily is not None and {"symbol", "date"} <= set(daily.columns) else {}
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
    # 原始昨收由共享预选路径提供; 这里只追加ATR指标, 已知比率同值按代码排序。
    ranked = metrics.drop("atr_raw_previous_close").sort(
        ["atr14_previous_close", "symbol"], descending=[True, False])
    ranked = ranked.with_columns(
        pl.Series("atr_rank", list(range(1, ranked.height + 1)), dtype=pl.Int64),
        pl.Series("atr_rank_score", [
            100. * (ranked.height - 1 - i) / (ranked.height - 1) if ranked.height > 1 else 50.
            for i in range(ranked.height)
        ], dtype=pl.Float64),
    )
    # 左连接保留所有合格预选股票, 包括ATR未知者, 并冻结可计算总数和逐股原因。
    result = rows.join(ranked, on="symbol", how="left", maintain_order="left").with_columns(
        pl.lit(ranked.height).alias("atr_rank_population"),
        pl.Series("atr_metric_error", [errors.get(symbol) for symbol in symbols], dtype=pl.String),
    )
    # 共享路径已有score时保留它; 没有score才以ATR排名分补充预选排序。
    return result if "score" in rows.columns else result.with_columns(pl.col("atr_rank_score").alias("score"))
