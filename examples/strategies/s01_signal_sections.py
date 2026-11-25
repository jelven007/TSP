"""s01/custom_mvxxcs01 的唯一自定义定义及候选评分实现。

基础过滤与执行声明来自本文件; 当前股票范围和触发器由保存覆盖决定,
入场/出场阈值及时间读取所选信号。当前逻辑统一维护于 docs/reference/s01.md。
此模块只返回数据, 不取上游、不写归档、不提交委托。

调用链: 盘后 select_rows -> enrich_selection 冻结前日资格及原始评分;
盘中引擎先注入所选信号, 再由 filter_minute_history -> selected_entries
投影冻结预选。无冻结预选的调用仍由 select_entries 适配。
"""
from datetime import datetime

import numpy as np
import polars as pl
from app.strategy.market_data import get_intraday_range_amount_score

# META 是执行能力声明; 信号参数属于关联保存定义, 不在这里复制其当前值。
META = {'id': 'custom_mvxxcs01',
 'name': 's01',
 'description': '尾盘走强入场, 下一交易日早盘清仓; 使用已完成的可用分钟累计值, 规则及时间读取所选信号。',
 'asset_types': ['stock'],
 'timeframes': ['1m'],
 'tags': ['隔夜', '低振幅', '分钟'],
 'daily_history_bars': 2,
 'params': [],
 'scoring': {'intraday_range_amount_score': 1},
 'scoring_mode': 'strategy',
 'order_by': 'score',
 'descending': True,
 'tie_break': 'symbol',
 'limit': 10000,
 'minute_execution': 'overnight_capacity_v1',
 'trigger_selection': 'optional_v1',
 'entry_window_policy': 'available_minutes_v1',
 'entry_phase_policy': 'independent_v1',
 'entry_breadth_observation': True,
 'entry_prior_ex_date_exclusion': True,
 # resolve_parameters 校验此基线结构; 通用信号注入后不再套用它的备用入场条件。
 'trigger_parameter_defaults': {'current_range_max': 0.0525,
                                'current_amount_min': 100000000,
                                'current_return_max': 0.03,
                                'current_return_min': 0.02,
                                'market_positive_min': 0.65,
                                'decline_3m': 0.002,
                                'guard_time': '11:26',
                                'vwap_buffer': 0.0003,
                                'sell_deadline': '13:30',
                                'return_15m_max': -0.003,
                                'peak_profit_min': 0.02,
                                'peak_retrace_min': 0.005,
                                'volume_ratio_min': 1.5},
 'entry_allocation': 'equal_selected_v1',
 'result_sections': 'signals'}
# 引用由保存覆盖解析; 引擎校验方向、启用状态和周期后才执行所选信号。
EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = ["csgi_late_session_strength_s01"]
EXIT_SIGNALS = ["csgi_morning_liquidation_s01"]
# 前交易日资格由共享 BASIC_FILTER 执行; 当日完整池只使用范围和状态字段。
BASIC_FILTER = {'boards': ['沪主板', '深主板', '创业板', '科创板'],
 'price_max': 100.0,
 'price_min': 12.0,
 'amount_max': None,
 'amount_min': 100000000.0,
 'exclude_st': True,
 'turnover_max': None,
 'turnover_min': None,
 'float_cap_max': None,
 'float_cap_min': None,
 'market_cap_min': None,
 'exclude_new_days': 60,
 'enabled': True,
 'data_reference': 'previous_trade_day',
 'change_pct_min': 4.0,
 'change_pct_max': 15.0,
 'amplitude_min': None,
 'amplitude_max': 12.0}
def parameters(values):
    """校验引擎已解析的适配参数; 本定义无独立用户参数默认表。

    此校验服务仍被调用的无冻结预选路径; 通用信号值在其模板入口校验。
    比例阈值为小数, 成交额为元, 适配时钟为 HH:MM; 不改写传入字典。
    """
    params = {**(values or {})}
    guard = minute_number(params["guard_time"])
    deadline = minute_number(params["sell_deadline"])
    momentum = params.get("_execution") == "momentum_capacity_v1"
    if not ((571 <= guard <= 690 or 781 <= guard <= 900) if momentum else 675 <= guard <= 688):
        raise ValueError("兜底信号时间必须在11:15~11:28")
    if not (guard <= deadline <= 900 if momentum else guard < deadline <= 900) or 691 <= deadline <= 780:
        raise ValueError("卖出截止必须晚于兜底信号且在收盘前的交易分钟, 不得位于午休")
    if not 0 <= params["market_positive_min"] <= 1:
        raise ValueError("市场上涨比例无效")
    for lower, upper in (("current_return_min", "current_return_max"),):
        if params[lower] > params[upper]:
            raise ValueError("隔夜区间下限不能超过上限")
    return params

def minute_number(value):
    """严格解析适配时钟为自午夜起分钟数, 非法时间直接抛错。"""
    text = str(value)
    parsed = datetime.strptime(text, "%H:%M")
    return parsed.hour * 60 + parsed.minute

def minute_frame(frame):
    """转换为北京时间无时区标签并排序, bar 是分钟数而非已完成证明。"""
    if frame.is_empty():
        return frame
    stamp = pl.col("datetime")
    if frame.schema["datetime"].time_zone is not None:
        stamp = stamp.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    return frame.with_columns(
        stamp.alias("datetime"),
        (stamp.dt.hour().cast(pl.Int32) * 60 + stamp.dt.minute().cast(pl.Int32)).alias("bar"),
    ).sort("symbol", "datetime")

def select_entries(frame, values, daily=None):
    """无冻结预选时的候选适配; 缺分钟/两日日线输入返回同 schema 空表。

    最近两条前日日线用于本路径的价格参考。当前独立个股任务使用下方
    selected_entries 的精确冻结昨收, 不在此重复筛选或计算完整池。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "close": pl.Float64, "score": pl.Float64})
    params = parameters(values)
    if frame.is_empty() or daily is None or daily.is_empty():
        return empty
    frame = frame.unique(["symbol", "datetime"], keep="last", maintain_order=True)
    frame = minute_frame(frame)
    day = frame["datetime"].max().date()
    frame = frame.filter((pl.col("datetime").dt.date() == day) & (
        pl.col("bar").is_between(571, 690) | pl.col("bar").is_between(781, 885)))
    # 调用方提供完成分钟; 累计可用值, 重复时标保留最后一条, 不补造缺口。
    # count 只作为覆盖字段返回, 不参与资格判断。
    good = frame.group_by("symbol").agg(
        count=pl.len(),
        valid=pl.all_horizontal([
            pl.col(f).is_finite() & (pl.col(f) >= 0 if f in ("amount", "volume") else pl.col(f) > 0)
            for f in ("open", "high", "low", "close", "amount")
        ]).fill_null(False).all(),
        close=pl.col("close").last(), high=pl.col("high").max(), low=pl.col("low").min(),
        amount=pl.col("amount").sum(), last_datetime=pl.col("datetime").last(),
    ).filter(pl.col("valid"))
    history = daily.filter(pl.col("date") < day).sort("symbol", "date")
    if not {"raw_close", "raw_high", "raw_low", "amount"}.issubset(history.columns):
        return empty
    prior = history.group_by("symbol", maintain_order=True).tail(2)
    if prior.is_empty():
        return empty
    info = prior.group_by("symbol").agg(
        *[pl.col(f).last().alias(f) for f in ("name", "listing_age_days", "is_risk_warning",
                                             "prior_ex_date") if f in prior.columns],
        n=pl.len(), prior_close=pl.col("raw_close").last(),
        previous_close=pl.col("raw_close").first(), prior_high=pl.col("raw_high").last(),
        prior_low=pl.col("raw_low").last(), prior_amount=pl.col("amount").last(),
        age_increment=pl.lit(day) - pl.col("date").last(),
    ).filter(pl.col("n") == 2)
    if "listing_age_days" in info.columns:
        info = info.with_columns(
            (pl.col("listing_age_days") + pl.col("age_increment").dt.total_days()).alias("listing_age_days"))
    good = good.join(info, on="symbol", how="inner").with_columns(
        (pl.col("close") / pl.col("prior_close") - 1).alias("current_return"),
        ((pl.col("high") - pl.col("low")) / pl.col("prior_close")).alias("current_range"),
        (pl.col("prior_close") / pl.col("previous_close") - 1).alias("prior_return"),
        ((pl.col("prior_high") - pl.col("prior_low")) / pl.col("previous_close")).alias("prior_range"),
    ).filter(pl.col("prior_close").is_finite() & (pl.col("prior_close") > 0))
    market = good.filter(pl.col("symbol").str.ends_with(".SH") | pl.col("symbol").str.ends_with(".SZ"))
    if not params.get("_s01_skip_entry") and (not market.height or (
        market.filter(pl.col("current_return") > 0).height / market.height < params["market_positive_min"]
    )):
        return empty
    # 前日数值条件由引擎 BASIC_FILTER 统一执行; 先用完整市场计算广度。
    condition = pl.lit(True)
    if not params.get("_s01_skip_entry"):
        condition &= (
            pl.col("current_return").is_between(params["current_return_min"], params["current_return_max"])
            & (pl.col("current_range") <= params["current_range_max"])
            & (pl.col("amount") >= params["current_amount_min"])
        )
    if "name" in good.columns:
        condition &= ~pl.col("name").fill_null("").str.to_uppercase().str.contains("ST")
    if "is_risk_warning" in good.columns:
        condition &= ~pl.col("is_risk_warning").fill_null(False)
    if "prior_ex_date" in good.columns:
        condition &= ~pl.col("prior_ex_date").fill_null(False)
    return get_intraday_range_amount_score(
        market.filter(condition), prev_close_column="prior_close",
    ).with_columns(
        pl.col("intraday_range_amount_score").alias("score"),
    ).sort(["score", "symbol"], descending=[True, False])


def filter_minute_history(df, params, *, daily=None, selection=None):
    """引擎候选入口; 已注入信号的冻结预选走投影, 其余走参数适配。

    _s01_skip_entry 由可选信号调用链注入, 避免备用阈值重复筛选。
    本入口不决定广度/个股阶段何时冻结, 也不直接发布最终买名单。
    """
    if selection is not None and params.get("_s01_skip_entry"):
        return selected_entries(df, selection)
    return select_entries(df, params, daily)

def selected_entries(frame, selection):
    """将冻结预选投影到已注入信号的真实分钟, 再计算当日原始分数。

    selection 含目标日和原始 reference_close; 截止由信号列与调用方
    完成分钟前缀决定。累计 OHLC/成交额逐股计算, 任一所用值无效则
    后续累计投影保持无效, 不能通过跳过坏行恢复资格。同股同分钟只取
    最后一条; 已记录前日除权除息者在此继续排除, 不扩大冻结预选名单。
    """
    empty = pl.DataFrame(schema={"symbol": pl.String, "close": pl.Float64, "score": pl.Float64})
    if frame.is_empty() or not selection["rows"]:
        return empty
    references = {row["symbol"]: row["reference_close"] for row in selection["rows"]
                  if not row.get("reference_prior_ex_date", False)
                  and isinstance(row.get("reference_close"), (int, float))
                  and np.isfinite(row["reference_close"]) and row["reference_close"] > 0}
    if not references:
        return empty
    frame = minute_frame(frame).unique(["symbol", "datetime"], keep="last", maintain_order=True)
    frame = frame.filter(
        pl.col("symbol").is_in(list(references))
        & (pl.col("datetime").dt.date().cast(pl.String) == selection["as_of"])
        & (pl.col("bar").is_between(571, 690) | pl.col("bar").is_between(781, 900)))
    valid = pl.all_horizontal([
        pl.col(f).is_finite() & (pl.col(f) >= 0 if f == "amount" else pl.col(f) > 0)
        for f in ("open", "high", "low", "close", "amount")
    ]).fill_null(False)
    frame = frame.with_columns(
        valid.cast(pl.Int32).cum_min().over("symbol").alias("_valid"),
        pl.col("high").cum_max().over("symbol"),
        pl.col("low").cum_min().over("symbol"),
        pl.col("amount").cum_sum().over("symbol"),
        pl.col("symbol").replace_strict(references, default=None, return_dtype=pl.Float64)
        .alias("prior_close"),
    )
    # 在完整前缀上先累计, 再取信号分钟; 等待确认不会移动累计右端点。
    signals = [name for name in frame.columns if name.startswith("csgi_")
               and frame.schema[name] == pl.Boolean]
    frame = (frame.filter(pl.any_horizontal(pl.col(name).fill_null(False) for name in signals))
             if signals else frame.filter(pl.col("bar") <= 885).group_by("symbol").tail(1))
    frame = frame.filter(pl.col("_valid") == 1).with_columns(
        (pl.col("close") / pl.col("prior_close") - 1).alias("current_return"),
        ((pl.col("high") - pl.col("low")) / pl.col("prior_close")).alias("current_range"),
        pl.col("datetime").alias("last_datetime"),
    )
    return get_intraday_range_amount_score(frame, prev_close_column="prior_close").with_columns(
        pl.col("intraday_range_amount_score").alias("score"),
    ).drop("_valid").sort(["score", "symbol"], descending=[True, False])


def enrich_selection(rows, params, *, daily, as_of):
    """盘后候选按原始振幅/成交额评分, 缺项保留未知而不删除成员。

    as_of 是目标日 T, rows 是 T-1 资格行, 振幅分母取精确 T-2 昨收;
    评分与盘中使用同一公式, 排序和归档由引擎及预选服务负责。
    """
    from app.strategy.market_data import previous_session
    reference = previous_session(previous_session(as_of))
    if "prev_close" not in rows.columns:
        prior = (daily.filter(pl.col("date") == reference).select(
            "symbol", pl.col("close").alias("prev_close")).unique("symbol", keep="last")
            if daily is not None and {"symbol", "date", "close"} <= set(daily.columns)
            else pl.DataFrame(schema={"symbol": pl.String, "prev_close": pl.Float64}))
        rows = rows.join(prior, on="symbol", how="left", maintain_order="left")
    return get_intraday_range_amount_score(rows).with_columns(
        pl.col("intraday_range_amount_score").alias("score"))
