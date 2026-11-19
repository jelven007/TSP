"""尾盘走强: 指定分钟的完整股票池广度与双周期 MA5, 纯计算不做 IO。"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time as clock
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

import polars as pl

from app.market_time import CN_TZ
from app.services.kline_periods import aggregate_minute_period

TEMPLATE = "late_session_strength"
OPTIONAL_KEYS = {"amount_min_100m", "turnover_min_pct", "amplitude_max_pct",
                 "volume_ratio_min", "end_time"}
DEFAULT_PARAMS = {
    "time": "14:30:00", "change_min_pct": 0.4, "change_max_pct": 4.0,
    "pool_up_min_pct": 60.0, "ma5_1m_count": 7, "ma5_5m_count": 4,
    "evaluation_delay_minutes": 0.5,
    **dict.fromkeys(OPTIONAL_KEYS),
}


def optional_definition(sig: dict) -> bool:
    raw = sig.get("params") or {}
    return isinstance(raw, dict) and (bool(OPTIONAL_KEYS & raw.keys())
                                     or any(value is None for value in raw.values()))


def parameters(sig: dict) -> dict:
    raw = sig.get("params") or {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_PARAMS):
        raise ValueError("尾盘走强参数格式错误或包含未知参数")
    params = {**DEFAULT_PARAMS, **raw}
    # Frozen six-field definitions keep their original scheduled minute.
    delay = raw.get("evaluation_delay_minutes", 0)
    if not (delay is None and optional_definition(sig)) and (isinstance(delay, bool) or not isinstance(delay, (int, float))
            or not math.isfinite(delay)
            or (delay != 0 and not (0.5 <= delay <= 15 and delay * 60 % 30 == 0))):
        raise ValueError("尾盘走强确认延迟须为30~900秒且为30秒的整数倍; 0秒仅兼容旧定义")
    params["evaluation_delay_minutes"] = delay
    if optional_definition(sig):
        return _optional_parameters(params, raw)
    earliest = time(13) if delay else time(13, 1)
    latest = (datetime.combine(date.min, time(15)) - timedelta(minutes=delay)).time()
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


def _optional_parameters(params: dict, raw: dict) -> dict:
    delay = raw.get("evaluation_delay_minutes", 0)
    params["evaluation_delay_minutes"] = delay
    for key in ("time", "end_time"):
        value = params[key]
        if value is None:
            continue
        try:
            at = time.fromisoformat(value)
            if at.tzinfo or at.strftime("%H:%M:%S") != value or not time(13) <= at <= time(15):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{key} 须为北京时间13:00:00~15:00:00") from None
    if params["time"] is not None:
        confirmation = datetime.combine(date.min, time.fromisoformat(params["time"])) + timedelta(minutes=delay or 0)
        if confirmation.date() != date.min or confirmation.time() > time(15):
            raise ValueError("确认时间不得晚于15:00:00")
        if params["end_time"] is not None and params["end_time"] < str(confirmation.time()):
            raise ValueError("结束时间不得早于确认时间")
    for key in ("pool_up_min_pct", "amount_min_100m", "turnover_min_pct",
                "amplitude_max_pct", "change_min_pct", "change_max_pct", "volume_ratio_min"):
        value = params[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or not .1 <= value <= 100):
            raise ValueError(f"{key} 须为0.1~100.0或为空")
    if (params["change_min_pct"] is not None and params["change_max_pct"] is not None
            and params["change_min_pct"] > params["change_max_pct"]):
        raise ValueError("当日涨幅下限不得大于上限")
    for key in ("ma5_1m_count", "ma5_5m_count"):
        if params[key] is not None and (type(params[key]) is not int or not 1 <= params[key] <= 100):
            raise ValueError(f"{key} 须为1~100的整数或为空")
    return params


def evaluation_time(sig: dict, day: date, *, completed_through: datetime | None = None) -> datetime | None:
    """实际检查时点, 评估、池公告观察、脉冲与回放共用。"""
    params = parameters(sig)
    if params["time"] is None:
        at = completed_through
    else:
        at = datetime.combine(day, time.fromisoformat(params["time"]))
    return at + timedelta(minutes=params["evaluation_delay_minutes"] or 0) if at is not None else None


def execution_end(sig: dict, day: date) -> datetime:
    """The saved end clock also bounds fills after a successful signal."""
    return datetime.combine(day, time.fromisoformat(parameters(sig)["end_time"] or "15:00:00"))


def bar_time(sig: dict, day: date, *, completed_through: datetime | None = None) -> datetime | None:
    """信号的启动分钟数据标签, 确认延迟不改变此截止。"""
    value = parameters(sig)["time"]
    if value is None:
        if completed_through is None:
            return None
        if completed_through.tzinfo:
            completed_through = completed_through.astimezone(CN_TZ).replace(tzinfo=None)
        return completed_through.replace(second=0, microsecond=0)
    at = time.fromisoformat(value).replace(second=0, microsecond=0)
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
    return min([fallback, *(at for at in times if at is not None)])


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
    latest = context.completed_through
    if latest is None and context.history is not None and "datetime" in context.history.columns:
        dt = pl.col("datetime")
        if context.history.schema["datetime"].time_zone:
            dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
        latest = context.history.select(dt.alias("datetime")).filter(
            (pl.col("datetime").dt.date() == context.as_of)
            & (pl.col("datetime") <= observed))["datetime"].max()
    if latest is not None and latest.tzinfo:
        latest = latest.astimezone(CN_TZ).replace(tzinfo=None)
    pending = []
    for sig in custom_signals.load_intraday_all(storage):
        if (sig.get("template") != TEMPLATE or sig.get("enabled") is False
                or custom_signals.intraday_column_name(sig["id"]) not in selected):
            continue
        available = evaluation_time(sig, context.as_of, completed_through=latest)
        if available is not None and observed < available:
            pending.append(available)
    return max(pending, default=None)


def entry_deadline(engine, strategy, overrides, context, *, expired_only=True) -> datetime | None:
    """User-specified live entry cutoff is 14:55 on the strategy session."""
    storage = engine._user_data_dir()
    observed = context.observed_at or context.completed_through
    if storage is None or observed is None:
        return None
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    from app.strategy import custom_signals

    selected = set(engine._effective_signals(overrides or {}, "entry_signals", strategy.entry_signals))
    deadlines = []
    for sig in custom_signals.load_intraday_all(storage):
        if (sig.get("template") != TEMPLATE or sig.get("enabled") is False
                or custom_signals.intraday_column_name(sig["id"]) not in selected):
            continue
        end = datetime.combine(context.as_of, time(14, 55))
        configured = parameters(sig)["end_time"]
        if configured is not None:
            end = min(end, datetime.combine(context.as_of, time.fromisoformat(configured)))
        if not expired_only or observed > end:
            deadlines.append(end)
    return min(deadlines, default=None)


@dataclass
class Evaluation:
    symbols: list[str] = field(default_factory=list)
    pool_up_pct: float | None = None
    diagnostics: list[str] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    stage: str = "waiting_start"
    pool_total: int = 0
    pool_usable: int = 0
    missing_pct: float = 0.0
    breadth_policy: str = "available_members_v1"
    ma_skipped_symbols: list[str] = field(default_factory=list)
    known_positive: int = 0
    missing_symbols: list[str] = field(default_factory=list)
    pool_up_pct_lower: float | None = None
    pool_up_pct_upper: float | None = None
    warnings: list[str] = field(default_factory=list)
    progress: dict = field(default_factory=dict)


def progress_plan(sig: dict, day: date, observed: datetime | None = None,
                  *, diagnostic_symbols: list[str] | None = None) -> dict:
    """Saved plan and receipts are separate; no wall-clock inference of execution."""
    params = parameters(sig)
    start = datetime.combine(day, time.fromisoformat(params["time"])) if params["time"] else None
    prefilter = start + timedelta(seconds=15) if start and (
        optional_definition(sig) or params["evaluation_delay_minutes"]) else start
    if observed is not None and observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    return {
        "schema": "late_session_progress_v1", "as_of": str(day),
        "cohort": "pool" if diagnostic_symbols is None else "frozen_selection",
        "observed_at": observed.isoformat() if observed is not None else None,
        "bar_at": bar_time(sig, day).isoformat() if params["time"] else None, "params": params,
        "steps": {key: {
            "state": "waiting", "scheduled_at": planned.isoformat() if planned else None,
            "started_at": None, "finished_at": None, "elapsed_ms": None,
            "processed": None, "total": None, "passed": None, "missing": None,
            "reason": "等待计算回执",
        } for key, planned in (
            ("breadth", prefilter), ("change", evaluation_time(sig, day) if optional_definition(sig) else prefilter),
            ("ma1", evaluation_time(sig, day)), ("ma5", evaluation_time(sig, day)),
        )},
    }


def _started() -> tuple[str, float]:
    return datetime.now(CN_TZ).replace(tzinfo=None).isoformat(), clock.perf_counter()


def _receipt(step: dict, started: tuple[str, float], **values) -> None:
    step.update(
        state="success", reason=None, started_at=started[0],
        finished_at=datetime.now(CN_TZ).replace(tzinfo=None).isoformat(),
        elapsed_ms=round((clock.perf_counter() - started[1]) * 1000, 3),
    )
    step.update(values)


def failed_progress(progress: dict, reason: str) -> dict:
    for key, step in progress["steps"].items():
        if step["state"] == "skipped":
            continue
        step.update(state="failed" if key in ("breadth", "change") else "skipped",
                    reason=reason)
    return progress


def _ready_symbols(frame: pl.DataFrame, symbols: set[str], end: datetime, count: int) -> set[str]:
    start = end - timedelta(minutes=count - 1)
    window = frame.filter(pl.col("symbol").is_in(sorted(symbols))
                          & pl.col("datetime").is_between(start, end))
    ready = window.group_by("symbol").agg(
        ((pl.len() == count) & (pl.col("datetime").n_unique() == count)
         & (pl.col("datetime").min() == start) & (pl.col("datetime").max() == end)
         & (pl.col("close").is_finite() & (pl.col("close") > 0)).fill_null(False).all()
         ).alias("_ready"))
    return set(ready.filter(pl.col("_ready"))["symbol"].to_list())


def _rising_symbols(frame: pl.DataFrame, end: datetime, count: int, step: int) -> set[str]:
    """N 个 MA5 需 N+4 根连续完成 K; 不跨午休, 不吞掉缺口。"""
    start = end - timedelta(minutes=(count + 3) * step)
    if not ((time(9, 31) <= start.time() <= end.time() <= time(11, 30))
            or (time(13, 1) <= start.time() <= end.time() <= time(15))):
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


def _evaluate_legacy(
    minute_df: pl.DataFrame, sig: dict, *, day: date,
    pool_symbols: list[str] | None, prev_close: dict[str, float],
    completed_through: datetime | None = None,
    diagnostic_symbols: list[str] | None = None,
) -> Evaluation:
    params = parameters(sig)
    result = Evaluation()
    result.progress = progress_plan(sig, day, completed_through,
                                    diagnostic_symbols=diagnostic_symbols)
    steps = result.progress["steps"]
    at = bar_time(sig, day)
    cutoff = None
    if completed_through is not None:
        cutoff = completed_through
        if cutoff.tzinfo:
            cutoff = cutoff.astimezone(CN_TZ).replace(tzinfo=None)
        start = datetime.combine(day, time.fromisoformat(params["time"]))
        prefilter_at = start + timedelta(seconds=15) if params["evaluation_delay_minutes"] else start
        if cutoff < prefilter_at:
            return result
    if sig.get("enabled") is False:
        for step in steps.values():
            step.update(state="skipped", reason="信号已停用")
        return result
    result.stage = "waiting_data"
    pool = sorted(set(pool_symbols or []))
    result.pool_total = len(pool)
    if not pool:
        result.diagnostics.append("尾盘走强缺少完整的当日股票池")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    if minute_df is None or not {"symbol", "datetime", "close"}.issubset(minute_df.columns):
        result.diagnostics.append("尾盘走强缺少分钟行情")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    breadth_started = _started()
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
    result.known_positive = sum(value > 0 for value in returns.values())
    result.missing_symbols = unknown
    result.pool_usable = len(returns)
    result.missing_pct = 100 * len(unknown) / len(pool)
    result.pool_up_pct_lower = 100 * result.known_positive / len(pool)
    result.pool_up_pct_upper = 100 * (result.known_positive + len(unknown)) / len(pool)
    if unknown:
        result.warnings.append(
            f"尾盘走强: 剔除 {len(unknown)}/{len(pool)} 只缺少有效指定分钟价格或昨收的股票, "
            f"按剩余 {len(returns)} 只计算上涨占比")
    if not returns:
        result.diagnostics.append("尾盘走强: 当日股票池剔除缺失数据后无有效股票, 无法计算上涨占比")
        failed_progress(result.progress, result.diagnostics[-1])
        _receipt(steps["breadth"], breadth_started, state="failed",
                 reason=result.diagnostics[-1], total=len(pool), processed=len(pool),
                 passed=0, missing=len(unknown), up_pct=None)
        return result
    # Stored universe remains complete; unavailable members leave only the breadth denominator.
    result.pool_up_pct = 100 * result.known_positive / len(returns)
    _receipt(steps["breadth"], breadth_started, total=len(pool), processed=len(pool),
             passed=result.known_positive, missing=len(unknown), usable=len(returns),
             up_pct=result.pool_up_pct, lower_pct=result.pool_up_pct_lower)
    change_started = _started()
    candidates = {symbol for symbol, value in returns.items()
                  if params["change_min_pct"] - 1e-10 <= value <= params["change_max_pct"] + 1e-10}
    cohort = set(pool) if diagnostic_symbols is None else set(diagnostic_symbols) & set(pool)
    _receipt(steps["change"], change_started, total=len(cohort), processed=len(cohort),
             passed=len(candidates & cohort), missing=len(cohort - returns.keys()),
             pool_passed=len(candidates))
    if result.pool_up_pct + 1e-10 < params["pool_up_min_pct"]:
        result.stage = "breadth_rejected"
        steps["breadth"].update(state="rejected", reason="上涨占比未达下限")
        for key in ("ma1", "ma5"):
            steps[key].update(state="skipped", reason="上涨占比未达下限")
        return result
    result.candidates = sorted(candidates)
    if cutoff is not None and cutoff < evaluation_time(sig, day):
        result.stage = "waiting_confirmation"
        return result
    result.stage = "confirmed"
    # Keep only the necessary afternoon window; reject duplicates before aggregation.
    minutes = max(params["ma5_1m_count"] + 4, (params["ma5_5m_count"] + 4)*5 + 4)
    frame = frame.filter(pl.col("datetime") > at - timedelta(minutes=minutes))
    end5 = at.replace(minute=(at.minute//5)*5)
    ma1_started = _started()
    ready1 = _ready_symbols(frame, candidates, at, params["ma5_1m_count"] + 4)
    unique = frame.group_by("symbol").agg((pl.len() == pl.col("datetime").n_unique()).alias("_unique"))
    unique_symbols = set(unique.filter(pl.col("_unique"))["symbol"].to_list())
    one_all = _rising_symbols(frame.filter(pl.col("symbol").is_in(sorted(ready1 & unique_symbols))),
                              at, params["ma5_1m_count"], 1)
    _receipt(steps["ma1"], ma1_started, total=len(candidates & cohort),
             processed=len(candidates & cohort), passed=len(one_all & cohort),
             missing=len((candidates - ready1) & cohort))
    ma5_started = _started()
    ready5 = _ready_symbols(frame, ready1, end5, (params["ma5_5m_count"] + 4) * 5)
    result.ma_skipped_symbols = sorted((candidates - ready1) | (ready1 - ready5))
    if result.ma_skipped_symbols:
        result.warnings.append(
            f"尾盘走强: 跳过 {len(result.ma_skipped_symbols)} 只缺少完整MA5分钟窗口的股票, 其余继续计算")
    # Per-symbol rolling operations commute with eligibility. Preserve the original
    # complete-window intersection while exposing each elimination separately.
    one = one_all & ready5
    frame = frame.filter(pl.col("symbol").is_in(sorted(ready1 & ready5 & unique_symbols)))
    if one:
        # All five constituent closes must be usable, not only the last close.
        valid = frame.filter(pl.col("symbol").is_in(sorted(one))
                             & pl.col("close").is_finite() & (pl.col("close") > 0))
        five = aggregate_minute_period(valid, "5m", complete_only=True)
        result.symbols = sorted(one & _rising_symbols(five, end5, params["ma5_5m_count"], 5))
    if one_all:
        _receipt(steps["ma5"], ma5_started, total=len(one_all & cohort),
                 processed=len(one_all & cohort), passed=len(set(result.symbols) & cohort),
                 missing=len((one_all - ready5) & cohort))
    else:
        steps["ma5"].update(state="skipped", reason="1分钟MA5无通过股票")
    return result


def needs_previous_close(sig: dict) -> bool:
    params = parameters(sig)
    return any(params[key] is not None for key in (
        "pool_up_min_pct", "change_min_pct", "change_max_pct", "amplitude_max_pct"))


def condition_inputs(context, sig: dict) -> dict:
    """Resolve only enabled conditions from caller-owned, point-in-time inputs."""
    params = parameters(sig)
    inputs = {}
    sources = [source for source in (context.daily_history, context.pool_history, context.current)
               if source is not None and not source.is_empty()]
    if params["turnover_min_pct"] is not None:
        shares = {}
        for source in [*sources, context.pool_universe]:
            if source is None or "symbol" not in source.columns:
                continue
            if "date" in source.columns:
                source = source.filter(pl.col("date") == context.as_of)
            if "float_shares" not in source.columns:
                if not {"volume", "turnover_rate"} <= set(source.columns):
                    continue
                # Recover the same row's effective capital from the shared enriched formula.
                # This supplies capital only; no daily price/volume is used as today's numerator.
                source = source.with_columns(
                    pl.when((pl.col("volume") > 0) & (pl.col("turnover_rate") > 0))
                    .then(pl.col("volume") * 10000 / pl.col("turnover_rate")).alias("float_shares"))
            for row in source.iter_rows(named=True):
                value = row["float_shares"]
                if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
                    shares[str(row["symbol"])] = value
        inputs["float_shares"] = shares
    if params["volume_ratio_min"] is not None:
        from app.strategy.basic_filter import previous_session

        expected = []
        prior = context.as_of
        for _ in range(5):
            prior = previous_session(prior)
            expected.append(prior)
        parts = []
        for source in sources:
            if not {"symbol", "date", "volume"}.issubset(source.columns):
                continue
            prior_rows = source.filter(pl.col("date").is_in(expected))
            rejected = prior_rows.group_by("symbol", "date").len().filter(pl.col("len") != 1)
            if not rejected.is_empty():
                prior_rows = prior_rows.join(rejected.select("symbol", "date"), on=["symbol", "date"], how="anti")
            if "quote_ts" in prior_rows.columns or "quote_time_basis" in prior_rows.columns:
                stamp = pl.col("quote_ts").cast(pl.Int64, strict=False) if "quote_ts" in prior_rows.columns else pl.lit(None)
                basis = pl.col("quote_time_basis") if "quote_time_basis" in prior_rows.columns else pl.lit(None)
                opening = pl.col("date").cast(pl.Datetime).dt.replace_time_zone("Asia/Shanghai").dt.timestamp("ms")
                batch = stamp.is_null() & basis.is_null()
                finalized = (basis == "source_snapshot") & stamp.is_between(
                    opening + 15 * 3600000, opening + 24 * 3600000 - 1)
                prior_rows = prior_rows.filter((batch | finalized).fill_null(False))
            parts.append(prior_rows.select("symbol", "date", pl.col("volume").cast(pl.Float64)))
        means = {}
        if parts:
            daily = pl.concat(parts, how="vertical_relaxed")
            # Identical copies from separate contexts are allowed; conflicting rows aren't.
            stats = daily.group_by("symbol", "date").agg(
                pl.col("volume").n_unique().alias("_distinct"),
                pl.col("volume").first().alias("volume"),
            ).group_by("symbol").agg(
                ((pl.len() == 5) & (pl.col("_distinct") == 1).all()
                 & (pl.col("volume").is_finite() & (pl.col("volume") >= 0)).fill_null(False).all()).alias("_valid"),
                pl.col("volume").mean().alias("_mean"),
            )
            means = {row["symbol"]: row["_mean"] for row in stats.filter(
                pl.col("_valid") & (pl.col("_mean") > 0)).iter_rows(named=True)}
        inputs["previous_volume_mean"] = means
    return inputs


def _evaluate_optional(
    minute_df: pl.DataFrame, sig: dict, *, day: date,
    pool_symbols: list[str] | None, prev_close: dict[str, float],
    completed_through: datetime | None = None, diagnostic_symbols: list[str] | None = None,
    float_shares: dict[str, float] | None = None,
    previous_volume_mean: dict[str, float] | None = None,
    breadth_only: bool = False, skip_breadth: bool = False,
) -> Evaluation:
    params = parameters(sig)
    result = Evaluation(progress=progress_plan(sig, day, completed_through,
                                               diagnostic_symbols=diagnostic_symbols))
    steps = result.progress["steps"]
    for key, enabled in (("breadth", params["pool_up_min_pct"] is not None),
                         ("change", any(params[name] is not None for name in (
                             "change_min_pct", "change_max_pct", "amount_min_100m",
                             "turnover_min_pct", "amplitude_max_pct", "volume_ratio_min"))),
                         ("ma1", params["ma5_1m_count"] is not None),
                         ("ma5", params["ma5_5m_count"] is not None)):
        if not enabled:
            steps[key].update(state="skipped", reason="参数为空, 不生效")
    if sig.get("enabled") is False:
        for step in steps.values():
            step.update(state="skipped", reason="信号已停用")
        return result
    observed = completed_through
    if observed is not None and observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    result.stage = "waiting_data"
    pool = sorted(set(pool_symbols or []))
    result.pool_total = len(pool)
    if not pool or minute_df is None or not {"symbol", "datetime", "close"}.issubset(minute_df.columns):
        result.diagnostics.append("尾盘走强缺少完整股票池或分钟行情")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    dt = pl.col("datetime")
    if minute_df.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    frame = minute_df.with_columns(dt.alias("datetime")).filter(
        pl.col("symbol").is_in(pool) & (pl.col("datetime").dt.date() == day)
        & ((pl.col("datetime").dt.time().is_between(time(9, 31), time(11, 30)))
           | (pl.col("datetime").dt.time().is_between(time(13, 1), time(15)))))
    if observed is not None:
        frame = frame.filter(pl.col("datetime") <= observed)
    latest = frame["datetime"].max() if not frame.is_empty() else None
    at = bar_time(sig, day, completed_through=latest)
    if at is None:
        return result
    check_at = evaluation_time(sig, day, completed_through=at)
    result.progress["bar_at"] = at.isoformat()
    if observed is not None and params["time"] and observed < datetime.combine(day, time.fromisoformat(params["time"])):
        result.stage = "waiting_start"
        return result
    if params["end_time"] is not None and (observed or check_at).time() > time.fromisoformat(params["end_time"]):
        result.stage = "expired"
        return result
    frame = frame.filter(pl.col("datetime") <= at).unique(
        ["symbol", "datetime"], keep="last", maintain_order=True).sort("symbol", "datetime")
    quotes = frame.filter(pl.col("datetime") == at)
    prices = {row["symbol"]: row["close"] for row in quotes.iter_rows(named=True)
              if isinstance(row["close"], (int, float)) and math.isfinite(row["close"]) and row["close"] > 0}
    returns = {symbol: (price / prev_close[symbol] - 1) * 100 for symbol, price in prices.items()
               if isinstance(prev_close.get(symbol), (int, float))
               and math.isfinite(prev_close[symbol]) and prev_close[symbol] > 0}
    valid = set(returns) if params["pool_up_min_pct"] is not None and not skip_breadth else set(prices)
    result.pool_usable = len(valid)
    result.missing_symbols = sorted(set(pool) - valid)
    result.missing_pct = 100 * len(result.missing_symbols) / len(pool)
    if not valid:
        result.diagnostics.append("尾盘走强完整池中无有效股票")
        failed_progress(result.progress, result.diagnostics[-1])
        return result
    if params["pool_up_min_pct"] is not None and not skip_breadth:
        started = _started()
        result.known_positive = sum(value > 0 for value in returns.values())
        result.pool_up_pct = 100 * result.known_positive / len(returns)
        result.pool_up_pct_lower = 100 * result.known_positive / len(pool)
        result.pool_up_pct_upper = 100 * (result.known_positive + len(result.missing_symbols)) / len(pool)
        _receipt(steps["breadth"], started, total=len(pool), processed=len(pool),
                 usable=len(returns), passed=result.known_positive,
                 missing=len(result.missing_symbols), up_pct=result.pool_up_pct)
        if result.pool_up_pct + 1e-10 < params["pool_up_min_pct"]:
            result.stage = "breadth_rejected"
            steps["breadth"].update(state="rejected", reason="上涨占比未达下限")
            return result
    if breadth_only:
        result.stage = "waiting_confirmation"
        return result
    started = _started()
    candidates = set(prices)
    for key, minimum in (("change_min_pct", True), ("change_max_pct", False)):
        if params[key] is not None:
            candidates &= {symbol for symbol, value in returns.items()
                           if (value + 1e-10 >= params[key] if minimum else value <= params[key] + 1e-10)}
    missing = (set(prices) - set(returns)) if any(
        params[key] is not None for key in ("change_min_pct", "change_max_pct")) else set()
    fields = set()
    if params["amount_min_100m"] is not None:
        fields.add("amount")
    if params["turnover_min_pct"] is not None or params["volume_ratio_min"] is not None:
        fields.add("volume")
    if params["amplitude_max_pct"] is not None:
        fields.update(("high", "low"))
    aggregates = {}
    if fields <= set(frame.columns) and fields:
        expressions = [(pl.col(key).is_finite() & (pl.col(key) >= 0)).fill_null(False).all().alias(f"valid_{key}")
                       for key in fields]
        expressions += [pl.col(key).sum().alias(key) for key in fields & {"amount", "volume"}]
        if "high" in fields:
            expressions += [pl.col("high").max().alias("high"), pl.col("low").min().alias("low"),
                            ((pl.col("high") >= pl.col("low")) & (pl.col("low") > 0)).fill_null(False).all().alias("_ohlc")]
        aggregates = {row["symbol"]: row for row in frame.group_by("symbol").agg(expressions).iter_rows(named=True)}
    from app.market_time import trading_minutes_elapsed_from_dt

    for symbol in list(candidates):
        row = aggregates.get(symbol, {})
        values = {}
        if params["amount_min_100m"] is not None:
            values["amount_min_100m"] = row.get("amount", 0) / 1e8 if row.get("valid_amount") else None
        if params["turnover_min_pct"] is not None:
            shares = (float_shares or {}).get(symbol)
            values["turnover_min_pct"] = (row["volume"] * 10000 / shares
                                         if row.get("valid_volume") and shares and math.isfinite(shares) and shares > 0 else None)
        if params["amplitude_max_pct"] is not None:
            previous = prev_close.get(symbol)
            values["amplitude_max_pct"] = (
                (row["high"] - row["low"]) / previous * 100
                if row.get("valid_high") and row.get("valid_low") and row.get("_ohlc")
                and previous and math.isfinite(previous) and previous > 0 else None)
        if params["volume_ratio_min"] is not None:
            mean = (previous_volume_mean or {}).get(symbol)
            elapsed = trading_minutes_elapsed_from_dt(at)
            values["volume_ratio_min"] = (row["volume"] * 240 / elapsed / mean
                                         if row.get("valid_volume") and mean and math.isfinite(mean)
                                         and mean > 0 and elapsed > 0 else None)
        for key, value in values.items():
            if value is None or not math.isfinite(value):
                missing.add(symbol)
                candidates.discard(symbol)
            elif (value > params[key] + 1e-10 if key == "amplitude_max_pct" else value + 1e-10 < params[key]):
                candidates.discard(symbol)
    cohort = set(pool) if diagnostic_symbols is None else set(diagnostic_symbols) & set(pool)
    if steps["change"]["state"] != "skipped":
        _receipt(steps["change"], started, total=len(cohort), processed=len(cohort),
                 passed=len(candidates & cohort), missing=len(missing & cohort), pool_passed=len(candidates))
    if set(prices) <= missing:
        result.diagnostics.append("尾盘走强: 全部有效价格成员缺少启用的个股条件数据")
        steps["change"].update(state="failed", reason=result.diagnostics[-1])
        return result
    result.candidates = sorted(candidates)
    if observed is not None and observed < check_at:
        result.stage = "waiting_confirmation"
        return result
    result.stage = "confirmed"
    for key, step_key, period in (("ma5_1m_count", "ma1", 1), ("ma5_5m_count", "ma5", 5)):
        count = params[key]
        if count is None:
            continue
        started = _started()
        end = at.replace(minute=(at.minute // period) * period)
        ready = _ready_symbols(frame, candidates, end, (count + 4) * period)
        selected = frame.filter(pl.col("symbol").is_in(sorted(ready)))
        if period == 5 and not selected.is_empty():
            selected = aggregate_minute_period(selected, "5m", complete_only=True)
        rising = _rising_symbols(selected, end, count, period) if not selected.is_empty() else set()
        result.ma_skipped_symbols.extend(sorted(candidates - ready))
        _receipt(steps[step_key], started, total=len(candidates & cohort), processed=len(candidates & cohort),
                 passed=len(rising & cohort), missing=len((candidates - ready) & cohort))
        if candidates and not ready:
            result.stage = "waiting_data"
            result.diagnostics.append(f"尾盘走强: 全部候选缺少{period}分钟MA5完整窗口")
            steps[step_key].update(state="failed", reason=result.diagnostics[-1])
            return result
        candidates &= rising
    result.symbols = sorted(candidates)
    return result


def evaluate(
    minute_df: pl.DataFrame, sig: dict, *, day: date,
    pool_symbols: list[str] | None, prev_close: dict[str, float],
    completed_through: datetime | None = None, diagnostic_symbols: list[str] | None = None,
    float_shares: dict[str, float] | None = None,
    previous_volume_mean: dict[str, float] | None = None,
    task_state: dict | None = None,
    input_error: str | None = None,
    breadth_only: bool = False,
) -> Evaluation:
    """Two-stage receipts; storage owners commit task_state with the result."""
    if not optional_definition(sig):
        return _evaluate_legacy(
            minute_df, sig, day=day, pool_symbols=pool_symbols, prev_close=prev_close,
            completed_through=completed_through, diagnostic_symbols=diagnostic_symbols)
    params = parameters(sig)
    state = task_state if task_state is not None else {}
    if (input_error and pool_symbols is None and state.get("as_of") == str(day)
            and state.get("params") == params and state.get("signal_id") == sig.get("id")):
        # Failed IO is not evidence that the full pool changed.
        pool_symbols = state.get("pool_symbols")
    observed = completed_through
    if observed is not None and observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    progress = progress_plan(sig, day, observed, diagnostic_symbols=diagnostic_symbols)
    for key, enabled in (("breadth", params["pool_up_min_pct"] is not None),
                         ("change", any(params[name] is not None for name in (
                             "change_min_pct", "change_max_pct", "amount_min_100m",
                             "turnover_min_pct", "amplitude_max_pct", "volume_ratio_min"))),
                         ("ma1", params["ma5_1m_count"] is not None),
                         ("ma5", params["ma5_5m_count"] is not None)):
        if not enabled:
            progress["steps"][key].update(state="skipped", reason="参数为空, 不生效")
    result = Evaluation(progress=progress, pool_total=len(set(pool_symbols or [])))
    if sig.get("enabled") is False:
        for step in progress["steps"].values():
            step.update(state="skipped", reason="信号已停用")
        return result
    start = datetime.combine(day, time.fromisoformat(params["time"])) if params["time"] else None
    breadth_at = start + timedelta(seconds=15) if start else None
    confirm_at = evaluation_time(sig, day)
    inputs = dict(day=day, pool_symbols=pool_symbols, prev_close=prev_close,
                  completed_through=observed, diagnostic_symbols=diagnostic_symbols,
                  float_shares=float_shares, previous_volume_mean=previous_volume_mean)
    identity = hashlib.sha256(json.dumps(
        [str(day), sig.get("id"), params, sorted(set(pool_symbols or [])),
         None if start else str(bar_time(sig, day, completed_through=observed))],
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if state.get("identity") != identity:
        state.clear()
        state.update(identity=identity, as_of=str(day), params=params,
                     signal_id=sig.get("id"), pool_symbols=sorted(set(pool_symbols or [])))
    if state.get("terminal"):
        return Evaluation(**copy.deepcopy(state["terminal"]))
    if start and observed is not None and observed < start:
        result.stage = "waiting_start"
        return result
    if breadth_at and observed is not None and observed < breadth_at and params["pool_up_min_pct"] is not None:
        result.stage = "waiting_breadth"
        return result
    end = datetime.combine(day, time.fromisoformat(params["end_time"])) if params["end_time"] else None
    breadth_expired = (params["pool_up_min_pct"] is not None and not state.get("breadth")
                       and params["evaluation_delay_minutes"] and confirm_at is not None
                       and observed is not None and observed >= confirm_at
                       and (state.get("breadth_failed") or task_state is not None))
    if (end is not None and observed is not None and observed > end) or breadth_expired:
        result.stage = "expired"
        result.diagnostics = ["上涨占比在确认时点前未计算成功" if breadth_expired else "信号有效时段已结束"]
        failed_progress(progress, result.diagnostics[0])
        if not state.get("terminal"):
            state["terminal"] = asdict(result)
        return result
    def attempt(**phase):
        try:
            if input_error:
                raise ValueError(input_error)
            return _evaluate_optional(minute_df, sig, **phase, **inputs)
        except Exception as exc:
            failed = Evaluation(progress=copy.deepcopy(progress), stage="waiting_data",
                                pool_total=result.pool_total, diagnostics=[str(exc)])
            failed_progress(failed.progress, str(exc))
            return failed

    breadth = None
    if params["pool_up_min_pct"] is not None:
        if state.get("breadth"):
            breadth = Evaluation(**copy.deepcopy(state["breadth"]))
        else:
            breadth = attempt(breadth_only=True)
            if not breadth.diagnostics:
                state["breadth"] = asdict(breadth)
            else:
                state["breadth_failed"] = True
                state["retry_until"] = confirm_at.isoformat() if confirm_at else None
                state["retry_phase"] = "breadth"
        if breadth.diagnostics or breadth.stage == "breadth_rejected":
            if breadth.diagnostics:
                breadth.progress.update(failure_reason="; ".join(breadth.diagnostics), phase="breadth")
            if breadth.stage == "breadth_rejected":
                state["terminal"] = asdict(breadth)
            return breadth
    if breadth_only:
        return breadth or result
    if confirm_at and observed is not None and observed < confirm_at:
        result = breadth or result
        result.stage = "waiting_confirmation"
        return result
    result = attempt(skip_breadth=True)
    if breadth is not None:
        for name in ("pool_up_pct", "pool_up_pct_lower", "pool_up_pct_upper", "known_positive",
                     "pool_usable", "missing_pct", "missing_symbols"):
            setattr(result, name, copy.deepcopy(getattr(breadth, name)))
        result.progress["steps"]["breadth"] = copy.deepcopy(breadth.progress["steps"]["breadth"])
    if result.diagnostics:
        result.progress.update(failure_reason="; ".join(result.diagnostics), phase="stocks")
        state["retry_until"] = end.isoformat() if end else None
        state["retry_phase"] = "stocks"
    elif result.stage == "confirmed":
        result.stage = "confirmed" if result.symbols else "conditions_rejected"
        result.progress["confirmed_at"] = observed.isoformat() if observed else (
            confirm_at.isoformat() if confirm_at else result.progress["bar_at"])
        state["terminal"] = asdict(result)
        state.pop("retry_phase", None)
        state.pop("retry_until", None)
    return result


def next_task_time(sig: dict, day: date, observed: datetime, state: dict) -> datetime | None:
    """Next stage or retry wake-up, independent of the public quote sequence."""
    if not optional_definition(sig) or sig.get("enabled") is False:
        return None
    params = parameters(sig)
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    if params["time"] is None:
        return None
    if state.get("as_of") != str(day) or state.get("params") != params:
        state = {}
    if state.get("terminal"):
        return None
    end = datetime.combine(day, time(14, 55))
    if params["end_time"] is not None:
        end = min(end, datetime.combine(day, time.fromisoformat(params["end_time"])))
    if observed > end:
        return None
    start = datetime.combine(day, time.fromisoformat(params["time"]))
    planned = (start + timedelta(seconds=15)
               if params["pool_up_min_pct"] is not None and not state.get("breadth")
               else evaluation_time(sig, day))
    return max(observed, planned) if planned is not None else observed


def previous_closes(
    context, *, exact_session: bool = True, isolate_symbols: bool = False,
) -> dict[str, float]:
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

                rejected = require_final_daily_rows(prior, previous, isolate_symbols=isolate_symbols)
                if rejected:
                    context.reference_errors.update(rejected)
        elif {"volume", "amount"}.issubset(prior.columns):
            prior = prior.filter(
                (pl.col("volume") > 0) | (pl.col("amount") > 0)
                | (pl.col("volume").is_null() & pl.col("amount").is_null()))
        parts.append(prior.select("symbol", "date", pl.col(column).cast(pl.Float64, strict=False).alias("raw_close")))
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
            if not isolate_symbols:
                raise ValueError(f"{previous} {conflicting['symbol'][0]}昨收来源不一致")
            context.reference_errors.update({
                symbol: f"{previous} {symbol}昨收来源不一致" for symbol in conflicting["symbol"]
            })
    if isolate_symbols:
        prior = prior.filter(~pl.col("symbol").is_in(list(context.reference_errors)))
    return {str(row["symbol"]): row["raw_close"]
            for row in prior.unique("symbol", keep="last").iter_rows(named=True)}
