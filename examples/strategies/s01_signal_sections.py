"""自定义 S01, 内置低振幅隔夜规则的独立参数版本。"""
from datetime import datetime

import numpy as np
import polars as pl

from app.strategy.market_data import get_intraday_range_amount_score

META = {'id': 'custom_mvxxcs01',
 'name': 's01',
 'description': '参考内置S01: 使用截至14:45已完成的可用分钟累计值进行低振幅筛选, 不要求固定225根; 入场及出场按可选触发器执行, 默认次交易日完整早盘后退出, 11:26时间兜底。2026-03-30至2026-11-06样本内优化, 未获独立样本认证。',
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
 'trigger_parameter_defaults': {'market_min_valid': 1000,
                                'current_range_max': 0.0525,
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
 'entry_allocation': 'equal_selected_v1'}
EXECUTION_BACKEND = "minute_filter"
ENTRY_SIGNALS = ["csgi_late_session_strength_s01"]
EXIT_SIGNALS = ["csgi_morning_liquidation_s01"]
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
PARAMS = META["params"]
DEFAULTS = {p["id"]: p["default"] for p in PARAMS}

def parameters(values):
    params = {**DEFAULTS, **(values or {})}
    for definition in PARAMS:
        key = definition["id"]
        if definition["type"] == "string":
            continue
        value = float(params[key])
        if not np.isfinite(value) or (value < 0 and key != "return_15m_max"):
            raise ValueError("隔夜参数必须是有效非负数: " + key)
        if definition["type"] == "int" and value != int(value):
            raise ValueError("隔夜整数参数无效: " + key)
        params[key] = int(value) if definition["type"] == "int" else value
    guard = minute_number(params["guard_time"])
    deadline = minute_number(params["sell_deadline"])
    momentum = params.get("_execution") == "momentum_capacity_v1"
    if not ((571 <= guard <= 690 or 781 <= guard <= 900) if momentum else 675 <= guard <= 688):
        raise ValueError("兜底信号时间必须在11:15~11:28")
    if not (guard <= deadline <= 900 if momentum else guard < deadline <= 900) or 691 <= deadline <= 780:
        raise ValueError("卖出截止必须晚于兜底信号且在收盘前的交易分钟, 不得位于午休")
    if params["market_min_valid"] < 1 or params["market_positive_min"] > 1:
        raise ValueError("市场有效证券数和上涨比例无效")
    for lower, upper in (("current_return_min", "current_return_max"),):
        if params[lower] > params[upper]:
            raise ValueError("隔夜区间下限不能超过上限")
    return params

def minute_number(value):
    text = str(value)
    parsed = datetime.strptime(text, "%H:%M")
    return parsed.hour * 60 + parsed.minute

def minute_frame(frame):
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
    empty = pl.DataFrame(schema={"symbol": pl.String, "close": pl.Float64, "score": pl.Float64})
    params = parameters(values)
    if frame.is_empty() or daily is None or daily.is_empty():
        return empty
    frame = frame.unique(["symbol", "datetime"], keep="last", maintain_order=True)
    frame = minute_frame(frame)
    day = frame["datetime"].max().date()
    frame = frame.filter((pl.col("datetime").dt.date() == day) & (
        pl.col("bar").is_between(571, 690) | pl.col("bar").is_between(781, 885)))
    # 可用完成分钟累计; 重复时标保留最后一条, 不补造缺失分钟。
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
    if not params.get("_s01_skip_entry") and (market.height < params["market_min_valid"] or (
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


def filter_minute_history(df, params, *, daily=None):
    return select_entries(df, params, daily)


def enrich_selection(rows, params, *, daily, as_of):
    """盘后选股按保存源码的原始振幅/成交额分排序, 缺项保留未知。"""
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

META['result_sections'] = 'signals'
