"""信号分区共享入口: 冻结资格与分钟入场分开, 时钟由保存信号解析。

select_rows负责资格、参考字段和共享评分; 策略扩展仅追加指标。
本模块返回计算内容, 预选归档、实时任务汇合与页面投影由服务层负责。
"""
import polars as pl

from app.strategy import overlay
from app.strategy.basic_filter import previous_session
from app.strategy.scoring import (
    effective_scoring_directions,
    effective_strategy_scoring,
    materialize_scoring_columns,
)


def supports_signal_sections(strategy):
    """按执行后端和META能力识别信号分区, 不以名称或固定策略ID路由。

    普通minute_filter可接入; 声明minute_execution的策略须另满足可选触发器
    契约。返回值只表示分区能力, 不表示监控已启用或当天归档已生成。
    """
    from app.strategy.s01_signals import optional_triggers

    return (strategy.execution_backend == "minute_filter"
            and strategy.meta.get("result_sections") == "signals"
            and (not strategy.meta.get("minute_execution") or optional_triggers(strategy)))


def live_entry_deadline(engine, strategy, overrides, context, *, expired_only=True):
    """查询交易日实时新入场截止; 14:55 整秒可通过, 严格超过才过期。"""
    from datetime import datetime, time

    from app.market_time import CN_TZ
    from app.strategy.late_session_strength import entry_deadline
    from app.strategy.s01_signals import optional_triggers

    if not optional_triggers(strategy):
        return entry_deadline(engine, strategy, overrides, context, expired_only=expired_only)
    observed = context.observed_at or context.completed_through
    if observed is None:
        return None
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    deadline = datetime.combine(context.as_of, time(14, 55))
    return deadline if not expired_only or observed > deadline else None


def pending_entry_time(engine, strategy, overrides, context):
    """返回未到达的已选确认时点, 让外层区分等待与成功零命中。"""
    from app.strategy.late_session_strength import pending_evaluation_time
    from app.strategy.s01_signals import entry_clock, optional_triggers, trigger_definitions

    generic_pending = pending_evaluation_time(engine, strategy, overrides, context)
    if not optional_triggers(strategy):
        return generic_pending
    from app.market_time import CN_TZ

    observed = context.observed_at or context.completed_through
    if observed is None:
        return None
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    clocks = [
        entry_clock(sig, context.as_of)
        for sig in trigger_definitions(engine, strategy, overrides)["entry"]
        if sig.get("template") == "s01_late_entry"
    ]
    native_pending = min((stamp for start, _, confirmation in clocks
                          for stamp in (start, confirmation) if observed < stamp), default=None)
    return min((stamp for stamp in (native_pending, generic_pending) if stamp is not None),
               default=None)


def select_rows(engine, strategy, context, *, overrides, pool=None, scope_only=False):
    """形成全部合格预选及精确参考字段, 不按展示上限截断。

    定版证据先验证再投影; 评分缺值只影响排序, 不删除资格。策略补充函数
    只能追加指标列, 成员、共享参考和已有 score 由本路径持有, 禁止覆盖。
    context.as_of为资格目标日; scope_only跳过额外overlay候选条件, 仍执行
    基础数值过滤和评分。正常筛空返回[], 输入/扩展契约错误向上抛出。
    scope_only 是冻结前日资格的入口, 不预先套用次日分钟入场信号。
    声明entry_prior_ex_date_exclusion的策略还按reference_day的除权标记
    排除资格, 在评分/补充指标前执行且scope_only仍生效; 未声明不增加此条件。
    """
    bf = engine._resolve_filter_scope({**strategy.basic_filter, **overrides.get("basic_filter", {})})
    current = context.current
    history = context.daily_history
    if current is None or current.is_empty():
        raise ValueError("选股结果缺少股票范围和数值过滤快照")
    if bf.get("data_reference") == "previous_trade_day":
        from app.services.daily_close_validation import require_final_daily_rows

        source_day = previous_session(context.as_of)
        reference = history if history is not None and not history.is_empty() else current
        require_final_daily_rows(reference.filter(pl.col("date") == source_day), source_day)
        if "date" in current.columns:
            completed_current = current.filter(pl.col("date") < context.as_of)
            if not completed_current.is_empty():
                require_final_daily_rows(completed_current, source_day)
    # 参考日及定版时钟先校验, 再按代码投影; 不能靠去重掩盖重复日线证据。
    rows = current.unique("symbol", keep="last")
    rows = engine._apply_basic_filter(rows, bf, history=history, as_of=context.as_of)
    if pool is not None:
        rows = rows.filter(pl.col("symbol").is_in(pool))
    if not scope_only:
        conditions = overlay.effective_conditions(overrides)
        rows = overlay.filter_minute_candidates(rows, conditions, history=history, as_of=context.as_of)
    if rows.is_empty():
        return []

    # 买入涨幅读取冻结reference_close; ATR另用一致复权日线, 两种分母不可混用。
    reference_day = previous_session(context.as_of) if bf.get("data_reference") == "previous_trade_day" else context.as_of
    reference = history if reference_day != context.as_of else current
    if reference is not None and not reference.is_empty() and "date" in reference.columns:
        reference = reference.filter(pl.col("date") == reference_day)
        price = "raw_close" if "raw_close" in reference.columns else "close"
        columns = [pl.col(price).alias("reference_close")]
        # 标记来自上游绑定的公司行为输入, 按reference_day消费; 前日模式不使用目标日事件。
        # 此处只投影声明策略已有的标记; 缺列不自行读事件, null沿现有契约按False。
        if strategy.meta.get("entry_prior_ex_date_exclusion") and "prior_ex_date" in reference.columns:
            columns.append(pl.col("prior_ex_date").fill_null(False).alias("reference_prior_ex_date"))
        if "change_pct" in reference.columns:
            columns.append(pl.col("change_pct").alias("reference_change_pct"))
        if "turnover_rate" in reference.columns:
            columns.append(pl.col("turnover_rate").alias("reference_turnover_rate"))
        if "float_shares" in reference.columns:
            columns.append((pl.col(price) * pl.col("float_shares") / 1e8).alias("reference_float_cap"))
        rows = rows.join(reference.select("symbol", *columns).unique("symbol", keep="last"),
                         on="symbol", how="left")
    rows = rows.with_columns(pl.lit(str(reference_day)).alias("reference_date"))
    # 仅已标记True的成员退出资格; 基础数值过滤关闭或scope_only不关闭这项能力。
    # 这里过滤预选行, 不修改context共享输入或完整股票池; 排除后为空可正常返回。
    if strategy.meta.get("entry_prior_ex_date_exclusion") and "reference_prior_ex_date" in rows.columns:
        rows = rows.filter(~pl.col("reference_prior_ex_date").fill_null(False))
        if rows.is_empty():
            return []
    if history is not None and not history.is_empty() and "amount" in history.columns:
        completed = materialize_scoring_columns(
            history.filter(pl.col("date") < context.as_of).sort("symbol", "date"),
            ["amount_mean_20d"],
        ).filter(pl.col("date") == previous_session(context.as_of))
        if "amount_mean_20d" not in completed.columns:
            completed = completed.with_columns(pl.lit(None, dtype=pl.Float64).alias("amount_mean_20d"))
        rows = rows.join(completed.select("symbol", "amount_mean_20d").unique("symbol", keep="last"),
                         on="symbol", how="left")

    scoring = effective_strategy_scoring(strategy.meta, overrides)
    scored = engine._apply_scoring(rows, scoring, effective_scoring_directions(overrides))
    if "score" in scored.columns:
        # 评分缺失只影响顺序, 不删除已经通过数值过滤的资格。
        rows = rows.join(scored.select("symbol", "score"), on="symbol", how="left").sort(
            ["score", "symbol"], descending=[strategy.meta.get("descending", True), False],
            nulls_last=True,
        )
    else:
        rows = rows.sort("symbol")
    enrich = getattr(strategy, "enrich_selection_fn", None)
    if enrich is not None:
        enriched = enrich(rows.clone(), engine.resolve_params(strategy, None, overrides),
                          daily=history, as_of=context.as_of)
        if (not isinstance(enriched, pl.DataFrame) or "symbol" not in enriched.columns
                or enriched.height != rows.height
                or enriched["symbol"].n_unique() != rows.height
                or set(enriched["symbol"]) != set(rows["symbol"])):
            raise ValueError("策略预选补充指标不得增删或重复选股成员")
        # 仅追加指标新列, 资格/参考/已有score由共享路径拥有; S02买排序复用atr_rank。
        extra = [name for name in enriched.columns if name not in rows.columns]
        rows = rows.join(enriched.select("symbol", *extra), on="symbol", how="left")
    if "score" in rows.columns:
        rows = rows.sort(["score", "symbol"],
                         descending=[True if scope_only else strategy.meta.get("descending", True), False],
                         nulls_last=True)
    return rows.to_dicts()
