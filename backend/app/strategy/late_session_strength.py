"""尾盘走强: 指定分钟的完整股票池广度与双周期 MA5, 纯计算不做 IO。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

import polars as pl

from app.market_time import CN_TZ
from app.services.kline_periods import aggregate_minute_period

TEMPLATE = "late_session_strength"
DEFAULT_PARAMS = {
    "time": "14:30:00", "change_min_pct": 0.4, "change_max_pct": 4.0,
    "pool_up_min_pct": 60.0, "ma5_1m_count": 7, "ma5_5m_count": 4,
    "evaluation_delay_minutes": 1,
}


def parameters(sig: dict) -> dict:
    raw = sig.get("params") or {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_PARAMS):
        raise ValueError("尾盘走强参数格式错误或包含未知参数")
    params = {**DEFAULT_PARAMS, **raw}
    # Frozen six-field definitions keep their original scheduled minute.
    delay = raw.get("evaluation_delay_minutes", 0)
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay not in (0, 1, 1.5):
        raise ValueError("尾盘走强检查延迟须为 0、60 或 90 秒")
    params["evaluation_delay_minutes"] = delay
    earliest, latest = (time(13), time(14, 59)) if delay else (time(13, 1), time(15))
    if delay == 1.5:
        latest = time(14, 58)
    try:
        at = time.fromisoformat(params["time"])
        if (at.strftime("%H:%M:%S") != params["time"] or at.tzinfo
                or (delay == 0 and at.second)):
            raise ValueError
        if not earliest <= at <= latest:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError(
            f"尾盘走强启动时间须为午后 {earliest}~{latest}; "
            "延迟确认定义支持秒, 无延迟旧定义秒数须为 00"
        ) from None
    for key in ("change_min_pct", "change_max_pct", "pool_up_min_pct"):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("涨幅与上涨占比须为有限数值")
    if not 0 <= params["change_min_pct"] <= params["change_max_pct"] <= 100:
        raise ValueError("涨幅范围须满足 0 <= 下限 <= 上限 <= 100%")
    if not 0 <= params["pool_up_min_pct"] <= 100:
        raise ValueError("股票池上涨占比须在 0~100%")
    for key, maximum in (("ma5_1m_count", 120), ("ma5_5m_count", 24)):
        if type(params[key]) is not int or not 2 <= params[key] <= maximum:
            raise ValueError(f"{key} 须为 2~{maximum} 的整数")
    return params


def evaluation_time(sig: dict, day: date) -> datetime:
    """实际检查时点, 评估、池公告观察、脉冲与回放共用。"""
    params = parameters(sig)
    return datetime.combine(day, time.fromisoformat(params["time"])) + timedelta(
        minutes=params["evaluation_delay_minutes"])


def bar_time(sig: dict, day: date) -> datetime:
    """信号的启动分钟数据标签, 确认延迟不改变此截止。"""
    at = time.fromisoformat(parameters(sig)["time"]).replace(second=0, microsecond=0)
    return datetime.combine(day, at)


def pool_cutoff(engine, strategy_ids, overrides_map, day: date, fallback: datetime) -> datetime:
    """A full-pool signal cannot exclude securities using a later publication."""
    from app.strategy import custom_signals

    getter = getattr(engine, "_user_data_dir", None)
    storage = getter() if callable(getter) else None
    if storage is None or not callable(getattr(engine, "get", None)):
        return fallback
    requested = set()
    for sid in strategy_ids:
        strategy = engine.get(sid)
        override = (overrides_map or {}).get(sid) or {}
        requested.update(override.get("entry_signals", strategy.entry_signals) or [])
        requested.update(strategy.required_features)
    times = [evaluation_time(sig, day)
             for sig in custom_signals.load_intraday_all(storage)
             if sig.get("template") == TEMPLATE and sig.get("enabled") is not False
             and custom_signals.intraday_column_name(sig["id"]) in requested]
    return min([fallback, *times])


def pending_evaluation_time(engine, strategy, overrides, context) -> datetime | None:
    """Return the latest selected delayed entry evaluation that is still pending."""
    observed = context.observed_at or context.completed_through
    storage = engine._user_data_dir()
    if observed is None or storage is None:
        return None
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    from app.strategy import custom_signals

    selected = set(engine._effective_signals(
        overrides or {}, "entry_signals", strategy.entry_signals,
    ))
    pending = [
        evaluation_time(sig, context.as_of)
        for sig in custom_signals.load_intraday_all(storage)
        if sig.get("template") == TEMPLATE and sig.get("enabled") is not False
        and custom_signals.intraday_column_name(sig["id"]) in selected
        and observed < evaluation_time(sig, context.as_of)
    ]
    return max(pending, default=None)


@dataclass
class Evaluation:
    symbols: list[str] = field(default_factory=list)
    pool_up_pct: float | None = None
    diagnostics: list[str] = field(default_factory=list)


def _rising_symbols(frame: pl.DataFrame, end: datetime, count: int, step: int) -> set[str]:
    """N 个 MA5 需 N+4 根连续完成 K; 不跨午休, 不吞掉缺口。"""
    start = end - timedelta(minutes=(count + 3) * step)
    if start.time() <= time(13):
        return set()
    window = frame.filter(pl.col("datetime").is_between(start, end)).sort("symbol", "datetime")
    if window.is_empty():
        return set()
    window = window.with_columns(pl.col("close").rolling_mean(5).over("symbol").alias("_ma"))
    stats = window.group_by("symbol").agg(
        ((pl.len() == count + 4)
         & (pl.col("datetime").n_unique() == count + 4)
         & (pl.col("datetime").min() == start) & (pl.col("datetime").max() == end)
         & (pl.col("datetime").diff().drop_nulls().min() == timedelta(minutes=step))
         & (pl.col("datetime").diff().drop_nulls().max() == timedelta(minutes=step))
         & (pl.col("close").is_finite() & (pl.col("close") > 0)).fill_null(False).all()
         & (pl.col("_ma").count() == count)
         & (pl.col("_ma").diff().drop_nulls() > 1e-10).all()).alias("_rising"))
    return set(stats.filter(pl.col("_rising"))["symbol"].to_list())


def evaluate(
    minute_df: pl.DataFrame, sig: dict, *, day: date,
    pool_symbols: list[str] | None, prev_close: dict[str, float],
    completed_through: datetime | None = None,
) -> Evaluation:
    params = parameters(sig)
    result = Evaluation()
    at = bar_time(sig, day)
    if completed_through is not None:
        cutoff = completed_through
        if cutoff.tzinfo:
            cutoff = cutoff.astimezone(CN_TZ).replace(tzinfo=None)
        if cutoff < evaluation_time(sig, day):
            return result
    if sig.get("enabled") is False:
        return result
    pool = sorted(set(pool_symbols or []))
    if not pool:
        result.diagnostics.append("尾盘走强缺少完整的当日股票池")
        return result
    if minute_df is None or not {"symbol", "datetime", "close"}.issubset(minute_df.columns):
        result.diagnostics.append("尾盘走强缺少分钟行情")
        return result
    dt = pl.col("datetime")
    if minute_df.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    frame = minute_df.with_columns(dt.alias("datetime")).filter(
        pl.col("symbol").is_in(pool) & (pl.col("datetime").dt.date() == day)
        & (pl.col("datetime") <= at) & (pl.col("datetime").dt.time() > time(13)))
    quotes = frame.filter(pl.col("datetime") == at)
    quotes_by_symbol = quotes.partition_by("symbol", as_dict=True)
    returns: dict[str, float] = {}
    unknown = []
    for symbol in pool:
        quote = quotes_by_symbol.get((symbol,))
        previous = prev_close.get(symbol)
        if (quote is None or quote.height != 1 or previous is None or not math.isfinite(previous) or previous <= 0
                or quote["close"][0] is None or not math.isfinite(quote["close"][0])
                or quote["close"][0] <= 0):
            unknown.append(symbol)
        else:
            returns[symbol] = (quote["close"][0] / previous - 1) * 100
    if unknown:
        result.diagnostics.append(f"尾盘走强: 股票池中 {len(unknown)}/{len(pool)} 只缺少指定分钟价格或昨收")
        return result
    result.pool_up_pct = 100 * sum(value > 0 for value in returns.values()) / len(pool)
    if result.pool_up_pct + 1e-10 < params["pool_up_min_pct"]:
        return result
    # Keep only the necessary afternoon window; reject duplicates before aggregation.
    minutes = max(params["ma5_1m_count"] + 4, (params["ma5_5m_count"] + 4)*5 + 4)
    frame = frame.filter(pl.col("datetime") > at - timedelta(minutes=minutes))
    end5 = at.replace(minute=(at.minute//5)*5)
    candidates = {symbol for symbol, value in returns.items()
                  if params["change_min_pct"] - 1e-10 <= value <= params["change_max_pct"] + 1e-10}
    unique = frame.group_by("symbol").agg((pl.len() == pl.col("datetime").n_unique()).alias("_unique"))
    candidates &= set(unique.filter(pl.col("_unique"))["symbol"].to_list())
    frame = frame.filter(pl.col("symbol").is_in(sorted(candidates)))
    one = _rising_symbols(frame, at, params["ma5_1m_count"], 1)
    if one:
        # All five constituent closes must be usable, not only the last close.
        valid = frame.filter(pl.col("symbol").is_in(sorted(one))
                             & pl.col("close").is_finite() & (pl.col("close") > 0))
        five = aggregate_minute_period(valid, "5m", complete_only=True)
        result.symbols = sorted(one & _rising_symbols(five, end5, params["ma5_5m_count"], 5))
    return result


def previous_closes(context, *, exact_session: bool = True) -> dict[str, float]:
    """昨收使用严格早于信号日的未复权价格, 不读取当日收盘。"""
    previous = None
    if exact_session:
        from app.strategy.basic_filter import previous_session

        previous = previous_session(context.as_of)
    parts = []
    for source in (context.current, context.daily_history, context.pool_history):
        if source is None or source.is_empty() or not {"symbol", "date"}.issubset(source.columns):
            continue
        column = "raw_close" if "raw_close" in source.columns else "close"
        if column not in source.columns:
            continue
        prior = source.filter(pl.col("date") < context.as_of)
        if exact_session:
            prior = prior.filter(pl.col("date") == previous)
            if not prior.is_empty():
                from app.services.daily_close_validation import require_final_daily_rows

                require_final_daily_rows(prior, previous)
        elif {"volume", "amount"}.issubset(prior.columns):
            prior = prior.filter(
                (pl.col("volume") > 0) | (pl.col("amount") > 0)
                | (pl.col("volume").is_null() & pl.col("amount").is_null()))
        parts.append(prior.select("symbol", "date", pl.col(column).alias("raw_close")))
    if not parts:
        if exact_session:
            raise ValueError(f"{previous}缺少准确前交易日定版昨收")
        return {}
    prior = pl.concat(parts, how="vertical_relaxed").filter(
        pl.col("raw_close").is_finite() & (pl.col("raw_close") > 0)).sort("date", maintain_order=True)
    if exact_session and prior.is_empty():
        raise ValueError(f"{previous}缺少准确前交易日定版昨收")
    if exact_session:
        conflicting = prior.group_by("symbol").agg(pl.col("raw_close").n_unique().alias("_prices")).filter(pl.col("_prices") > 1)
        if not conflicting.is_empty():
            raise ValueError(f"{previous} {conflicting['symbol'][0]}昨收来源不一致")
    return {str(row["symbol"]): row["raw_close"]
            for row in prior.unique("symbol", keep="last").iter_rows(named=True)}
