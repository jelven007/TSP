"""S01 trigger adapters; frozen legacy bindings retain their original behavior."""
from __future__ import annotations

import copy
import math
import re
from numbers import Real

TEMPLATES = {"entry": "s01_late_entry", "exit": "s01_morning_exit"}
ENTRY_KEYS = frozenset({
    "current_amount_min", "current_return_min", "current_return_max",
    "current_range_max", "market_min_valid", "market_positive_min",
})
EXIT_KEYS = frozenset({
    "vwap_buffer", "peak_profit_min", "peak_retrace_min", "return_15m_max",
    "decline_3m", "volume_ratio_min", "guard_time", "sell_deadline",
})
PARAMETER_KEYS = ENTRY_KEYS | EXIT_KEYS
TIMING_KEYS = frozenset({"time", "evaluation_delay_minutes"})
MARKET_KEYS = frozenset({"market_validity"})
MARKET_VALIDITY = "price_amount_prev_close_v2"
ENTRY_TIMING_DEFAULTS = {"time": "14:45:00", "evaluation_delay_minutes": 1}


def entry_clock(signal, day):
    """Start, completed input cutoff, confirmation; missing clock is a frozen legacy signal."""
    from datetime import datetime, time, timedelta

    values = signal_parameters(signal)
    start = datetime.combine(day, time.fromisoformat(values.get("time", "14:45:00")))
    return start, start.replace(second=0, microsecond=0), start + timedelta(
        minutes=values.get("evaluation_delay_minutes", 0))


def optional_triggers(strategy) -> bool:
    return (strategy.execution_backend == "minute_filter"
            and strategy.meta.get("minute_execution") == "overnight_capacity_v1"
            and strategy.meta.get("trigger_selection") == "optional_v1")


def stored_definitions(engine) -> list[dict]:
    from app.strategy import custom_signals

    scoped = custom_signals.scoped_definitions()
    if scoped is not None:
        return scoped
    storage = getattr(engine, "documents", None)
    if storage is None and engine is not None:
        storage = engine._user_data_dir()
    return custom_signals.load_all(storage) if storage is not None else []


def trigger_definitions(engine, strategy, overrides=None, *, strict=True) -> dict:
    """Validate optional selections before either saving or executing them."""
    from app.indicators.pipeline import LIMIT_SIGNAL_OUTPUTS, SIGNAL_DEPENDENCIES
    from app.strategy import custom_signals

    stored = stored_definitions(engine)
    output = {}
    for kind in TEMPLATES:
        key = kind + "_signals"
        names = (overrides or {}).get(key, getattr(strategy, key))
        if (not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names)
                or len(names) != len(set(names))):
            raise ValueError(f"S01{kind}触发器须为不重复的信号列表")
        selected = []
        for name in names:
            try:
                if name.startswith("signal_"):
                    if name not in SIGNAL_DEPENDENCIES and name not in LIMIT_SIGNAL_OUTPUTS:
                        raise ValueError(f"信号{name}不存在")
                    selected.append({"id": name, "column": name, "builtin": True})
                    continue
                prefix = "csgi_" if name.startswith("csgi_") else "csg_"
                if not name.startswith(prefix):
                    raise ValueError(f"信号{name}引用无效")
                matches = [s for s in stored if s.get("id") == name[len(prefix):]]
                if len(matches) != 1:
                    raise ValueError(f"信号{name}不存在或重复")
                sig = matches[0]
                custom_signals.validate(sig)
                if sig.get("enabled") is not True or sig.get("kind") not in (kind, "both"):
                    raise ValueError(f"信号{name}方向不匹配或已停用")
                column = (custom_signals.intraday_column_name(sig["id"])
                          if sig.get("timeframe") == "intraday" else custom_signals.column_name(sig["id"]))
                if column != name:
                    raise ValueError(f"信号{name}周期不匹配")
                selected.append({**copy.deepcopy(sig), "column": column})
            except ValueError as exc:
                if strict:
                    raise ValueError(f"S01触发器不可用: {exc}") from exc
                selected.append({"id": name, "column": name, "enabled": False, "error": str(exc)})
        output[kind] = selected
    return output


def signal_parameters(signal: dict) -> dict:
    """Validate complete stored values without silently supplying old defaults."""
    kind = next((k for k, template in TEMPLATES.items() if signal.get("template") == template), None)
    if kind is None or signal.get("kind") != kind or signal.get("timeframe") != "intraday":
        raise ValueError("S01信号模板须匹配盘中入场或出场类型")
    if signal.get("conditions"):
        raise ValueError("S01信号使用专用参数, 不能混入普通条件")
    keys = ENTRY_KEYS if kind == "entry" else EXIT_KEYS
    values = signal.get("params")
    if (not isinstance(values, dict) or set(values) not in (
            (keys, keys | TIMING_KEYS, keys | TIMING_KEYS | MARKET_KEYS)
            if kind == "entry" else (keys,))):
        raise ValueError("S01信号参数缺失或包含不支持的参数")
    result = dict(values)
    for key in keys - {"guard_time", "sell_deadline"}:
        raw = values[key]
        if isinstance(raw, bool) or not isinstance(raw, Real) or not math.isfinite(raw):
            raise ValueError(f"S01参数{key}须为有效数值")
        if raw < 0 and key != "return_15m_max":
            raise ValueError(f"S01参数{key}不能为负数")
        result[key] = float(raw)
    if kind == "entry":
        if "market_validity" in values and values["market_validity"] != MARKET_VALIDITY:
            raise ValueError("S01市场有效证券口径无效")
        if values.keys() >= TIMING_KEYS:
            from datetime import date, datetime, time, timedelta

            delay = values["evaluation_delay_minutes"]
            if (isinstance(delay, bool) or not isinstance(delay, Real) or not math.isfinite(delay)
                    or not .5 <= delay <= 15 or delay * 60 % 30):
                raise ValueError("S01确认延时须为30~900秒且为30秒的整数倍")
            try:
                at = time.fromisoformat(values["time"])
                latest = (datetime.combine(date.min, time(15)) - timedelta(minutes=delay)).time()
                if (at.tzinfo or at.strftime("%H:%M:%S") != values["time"]
                        or not time(13, 1) <= at <= latest):
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("S01启动时间须为HH:MM:SS, 在13:01之后且确认不晚于15:00") from None
        count = result["market_min_valid"]
        if count < 1 or count != int(count):
            raise ValueError("S01市场有效证券下限须为正整数")
        result["market_min_valid"] = int(count)
        if result["market_positive_min"] > 1:
            raise ValueError("S01市场上涨比例须为0~1的小数")
        if result["current_return_min"] > result["current_return_max"]:
            raise ValueError("S01当日涨幅下限不能超过上限")
    else:
        from app.strategy.builtin._overnight import minute_number

        for key in ("guard_time", "sell_deadline"):
            if not isinstance(values[key], str) or not re.fullmatch(r"\d{2}:\d{2}", values[key]):
                raise ValueError("S01时间须为HH:MM整分钟")
        try:
            guard, deadline = (minute_number(values[k]) for k in ("guard_time", "sell_deadline"))
        except ValueError as exc:
            raise ValueError("S01时间无效") from exc
        if not 675 <= guard <= 688:
            raise ValueError("S01兜底信号时间须为11:15~11:28")
        if not guard < deadline <= 900 or 691 <= deadline <= 780:
            raise ValueError("S01成交截止须晚于兜底且不在午休或收盘后")
    return result


def bound_definitions(engine, strategy, *, overrides: dict | None = None, strict: bool = True) -> dict:
    """Resolve ordinary trigger references; retain old frozen META bindings."""
    if optional_triggers(strategy):
        selected = trigger_definitions(engine, strategy, overrides, strict=strict)
        output = {}
        for kind, signals in selected.items():
            native = next((s for s in signals if s.get("template") == TEMPLATES[kind]), None)
            if native is not None:
                output[kind] = {k: v for k, v in native.items() if k != "column"}
        return output
    legacy = strategy.meta.get("parameter_signals")
    if legacy is not None and (
        not isinstance(legacy, dict) or set(legacy) != set(TEMPLATES)
        or any(not isinstance(sid, str) or not sid for sid in legacy.values())
    ):
        raise ValueError("S01参数信号引用须包含entry/exit")
    overrides = overrides or {}
    columns = {}
    for kind in TEMPLATES:
        key = kind + "_signals"
        default = getattr(strategy, key) or (["csgi_" + legacy[kind]] if legacy else [])
        columns[kind] = overrides.get(key, default)
    compatible = (strategy.execution_backend == "minute_filter"
                  and strategy.meta.get("minute_execution") == "overnight_capacity_v1")
    configured = legacy is not None or (compatible and (
        strategy.entry_signals or strategy.exit_signals))
    if not configured and not any(
        isinstance(names, list) and any(str(name).startswith("csgi_") for name in names)
        for names in columns.values()
    ):
        return {}
    from app.strategy import custom_signals

    stored = custom_signals.scoped_definitions()
    if stored is None:
        storage = getattr(engine, "documents", None)
        if storage is None and engine is not None:
            storage = engine._user_data_dir()
        stored = custom_signals.load_all(storage) if storage is not None else []
    if not configured:
        s01_columns = {"csgi_" + s["id"] for s in stored if s.get("template") in TEMPLATES.values()}
        if not any(name in s01_columns for names in columns.values() if isinstance(names, list) for name in names):
            return {}
    if not compatible:
        raise ValueError("S01信号仅支持S01隔夜分钟执行")
    output = {}
    for kind, template in TEMPLATES.items():
        names = columns[kind]
        sid = names[0].removeprefix("csgi_") if (
            isinstance(names, list) and names and isinstance(names[0], str)) else ""
        matches = [s for s in stored if s.get("id") == sid]
        try:
            if (not isinstance(names, list) or len(names) != 1
                    or not isinstance(names[0], str) or not names[0].startswith("csgi_")):
                raise ValueError(f"S01{kind}触发器须选择一个对应的S01信号")
            if len(matches) != 1:
                raise ValueError(f"S01绑定信号{sid}不存在或重复")
            signal = matches[0]
            custom_signals.validate(signal)
            if signal.get("template") != template or signal.get("enabled") is not True:
                raise ValueError(f"S01绑定信号{sid}模板不匹配或已停用")
            signal_parameters(signal)
            output[kind] = copy.deepcopy(signal)
        except ValueError as exc:
            if strict:
                raise ValueError(f"S01参数信号不可用: {exc}") from exc
            output[kind] = {
                "id": sid, "name": sid, "kind": kind, "template": template,
                "timeframe": "intraday", "enabled": False, "conditions": [], "error": str(exc),
            }
    return output


def resolve_parameters(engine, strategy, values: dict, *, definitions: dict | None = None,
                       overrides: dict | None = None) -> dict:
    definitions = bound_definitions(engine, strategy, overrides=overrides) if definitions is None else definitions
    if optional_triggers(strategy):
        baseline = strategy.meta.get("trigger_parameter_defaults")
        if not isinstance(baseline, dict) or set(baseline) != PARAMETER_KEYS:
            raise ValueError("S01可选触发器缺少冻结参数基线")
        resolved = {**baseline, **{k: v for k, v in values.items()
                                  if k not in PARAMETER_KEYS | TIMING_KEYS | MARKET_KEYS}}
        for sig in definitions.values():
            resolved.update(signal_parameters(sig))
        return resolved
    if not definitions:
        return values
    resolved = {k: v for k, v in values.items() if k not in PARAMETER_KEYS | TIMING_KEYS | MARKET_KEYS}
    for signal in definitions.values():
        resolved.update(signal_parameters(signal))
    return resolved


def next_entry_evaluation_time(engine, strategy, overrides, context):
    """Worker wakeup visits both saved S01 stages, without enabling a monitor."""
    from app.market_time import CN_TZ

    observed = context.observed_at or context.completed_through
    if observed is None or not optional_triggers(strategy):
        return None
    if observed.tzinfo:
        observed = observed.astimezone(CN_TZ).replace(tzinfo=None)
    signals = trigger_definitions(engine, strategy, overrides)["entry"]
    stages = [at for sig in signals if sig.get("template") == TEMPLATES["entry"]
              and "time" in sig.get("params", {})
              for at in entry_clock(sig, context.as_of)[::2] if at > observed]
    return min(stages, default=None)


def timed_entry(signal, history, context, params, *, candidates_only=False):
    """Compute environment at start and stock thresholds only after confirmation."""
    import polars as pl

    from app.market_time import CN_TZ, cn_now, cn_today
    from app.strategy.builtin._overnight import minute_frame, select_entries

    start, cutoff, confirmation = entry_clock(signal, context.as_of)
    now = context.observed_at or context.completed_through
    if now is None:
        now = cn_now() if context.as_of == cn_today() else confirmation
    def normalize(t):
        return t.astimezone(CN_TZ).replace(tzinfo=None) if t.tzinfo else t

    now = normalize(now)
    quality = {"start_at": start.isoformat(), "bar_cutoff": cutoff.isoformat(),
               "confirmation_at": confirmation.isoformat(), "stage": "waiting_start"}
    context.intraday_data_quality["csgi_" + signal["id"]] = quality
    empty = pl.DataFrame(schema={"symbol": pl.String, "close": pl.Float64, "score": pl.Float64})
    if now < start:
        return empty
    completed = normalize(context.completed_through) if context.completed_through else now
    source = minute_frame(history).filter(
        (pl.col("datetime").dt.date() == context.as_of)
        & (pl.col("datetime") <= min(cutoff, now, completed))) if not history.is_empty() else history
    values = {**params, **signal_parameters(signal), "_s01_entry_bar": cutoff.hour * 60 + cutoff.minute,
              "_s01_external_basic": True}
    if values.get("market_validity") == MARKET_VALIDITY:
        from app.strategy.basic_filter import previous_session

        values["_s01_previous_day"] = previous_session(context.as_of)
    market = select_entries(source, {**values, "_s01_environment_only": True},
                            daily=context.daily_history)
    count = market.height
    up = market.filter(pl.col("current_return") > 0).height if count else 0
    passed = count >= values["market_min_valid"] and up / count >= values["market_positive_min"]
    quality.update(market_valid_count=count, market_positive_count=up,
                   market_positive_ratio=up / count if count else None, market_passed=passed,
                   stage="waiting_confirmation" if now < confirmation else "confirmed")
    if now < confirmation or (not candidates_only and not passed):
        return empty
    result = select_entries(source, {**values, "_s01_skip_entry": candidates_only},
                            daily=context.daily_history)
    return result.with_columns(pl.lit(confirmation).alias("entry_evaluated_at"))


def daily_signal_frame(context, definitions):
    """Daily triggers use only the previous completed session, never today's final bar."""
    import polars as pl

    from app.indicators.pipeline import compute_signals
    from app.strategy import custom_signals

    names = [s["column"] for s in definitions
             if s.get("builtin") or s.get("timeframe", "daily") != "intraday"]
    if not names:
        return pl.DataFrame()
    source = context.daily_history
    if source is None or source.is_empty() or "date" not in source.columns:
        raise ValueError("S01日线触发器缺少前交易日行情")
    source = source.filter(pl.col("date") < context.as_of).sort("symbol", "date")
    custom = [s for s in definitions if not s.get("builtin") and s.get("timeframe", "daily") != "intraday"]
    exprs = custom_signals.build_expressions(custom)
    source = custom_signals.materialize_factor_columns(source, exprs, needed=set(names))
    source = custom_signals.inject(source, exprs, needed=set(names))
    missing_builtin = {n for n in names if n.startswith("signal_") and n not in source.columns}
    if missing_builtin:
        source = compute_signals(source, needed=missing_builtin)
    missing = set(names) - set(source.columns)
    if missing:
        raise ValueError("S01触发器缺少信号数据: " + ", ".join(sorted(missing)))
    from app.strategy.basic_filter import previous_session

    return source.filter(pl.col("date") == previous_session(context.as_of)).select("symbol", *names)


def entry_frame(engine, strategy, candidates, history, context, params, definitions):
    """OR across independent native thresholds and completed generic trigger events."""
    from datetime import datetime, time

    import polars as pl

    from app.strategy.builtin._overnight import minute_frame

    if candidates.is_empty() or not definitions:
        return candidates
    output = candidates
    daily = daily_signal_frame(context, definitions)
    if not daily.is_empty():
        output = output.join(daily, on="symbol", how="left")
    for sig in definitions:
        column = sig["column"]
        if sig.get("template") == TEMPLATES["entry"]:
            if len(definitions) == 1 and column in output.columns:
                continue
            values = {k: v for k, v in params.items() if k != "_s01_skip_entry"}
            native = (timed_entry(sig, history, context, values) if "time" in sig["params"]
                      else strategy.filter_minute_history_fn(
                          history, {**values, **signal_parameters(sig)}, daily=context.daily_history))
            output = output.with_columns(pl.col("symbol").is_in(
                native["symbol"].to_list()).alias(column))
        elif sig.get("timeframe") == "intraday":
            if column not in history.columns:
                raise ValueError("S01触发器缺少信号数据: " + column)
            source = minute_frame(history).filter(
                (pl.col("datetime").dt.date() == context.as_of) & (pl.col("bar") <= 885))
            if "entry_evaluated_at" in source.columns:
                source = source.filter(pl.col("entry_evaluated_at").is_null()
                                       | (pl.col("entry_evaluated_at") <= datetime.combine(
                                           context.as_of, time(14, 45))))
            hits = source.filter(pl.col(column).fill_null(False))["symbol"].unique().to_list()
            output = output.with_columns(pl.col("symbol").is_in(hits).alias(column))
    return output.filter(pl.any_horizontal(
        pl.col(s["column"]).fill_null(False) for s in definitions))


def risk_exit_hits(context, policy, positions, frame, now):
    """Emit causal exit signals; execution still uses the next minute's capacity."""
    from datetime import date, datetime, timedelta

    import polars as pl

    from app.market_time import CN_TZ, published_trading_day
    from app.strategy.builtin._overnight import minute_frame, valid_rows
    from app.strategy.risk_policy import price_exit, valid_price

    hits = []
    trailing = (policy.trailing_stop_pct is not None
                or (policy.trailing_take_profit_activate_pct is not None
                    and policy.trailing_take_profit_drawdown_pct is not None))
    for position in positions:
        try:
            entry_day = date.fromisoformat(str(position.get("entry_date", ""))[:10])
            if entry_day >= context.as_of:
                continue
            elapsed = []
            for offset in range(1, (context.as_of - entry_day).days + 1):
                day = entry_day + timedelta(days=offset)
                trading = published_trading_day(day)
                if trading is None:
                    raise ValueError(f"{day}缺少有证据的交易日历")
                if trading:
                    elapsed.append(day)
            reason, bar = None, None
            if policy.max_hold_days and len(elapsed) >= policy.max_hold_days and now.hour * 60 + now.minute >= 570:
                reason, bar = "max_hold", 570
            entry = position.get("entry_unit_cost")
            if reason is None and not valid_price(entry):
                raise ValueError("价格风控缺少有效持仓成本")
            peak = position.get("risk_peak")
            if reason is None and trailing and not valid_price(peak):
                loader = context.holding_history_loader
                stamp = position.get("entry_time")
                if not callable(loader) or not stamp:
                    raise ValueError("移动风控缺少买入成交时点或持仓分钟历史")
                stamp = datetime.fromisoformat(str(stamp))
                if stamp.tzinfo:
                    stamp = stamp.astimezone(CN_TZ).replace(tzinfo=None)
                days = [entry_day, *[d for d in elapsed if d < context.as_of]]
                previous = minute_frame(loader([position["symbol"]], days, context.asset_type))
                if previous.is_empty():
                    raise ValueError("移动风控缺少持仓期间分钟历史")
                previous = previous.filter(
                    (pl.col("symbol") == position["symbol"])
                    & (pl.col("datetime") > stamp)
                    & (pl.col("datetime").dt.date() < context.as_of))
                for day in days:
                    source = previous.filter(pl.col("datetime").dt.date() == day)
                    required = [bar for bar in [*range(571, 691), *range(781, 901)]
                                if day > entry_day or bar > stamp.hour * 60 + stamp.minute]
                    rows = {row["bar"]: row for row in source.to_dicts()}
                    if (source.height != len(rows) or not valid_rows(
                            rows, required, ("open", "high", "low", "close"))):
                        raise ValueError(f"{day}移动风控缺少完整持仓分钟历史")
                # The buy minute's earlier high cannot become a holding peak.
                peak = entry if previous.is_empty() else max(entry, previous["high"].max())
            peak = peak if valid_price(peak) else entry
            history = frame.filter(pl.col("symbol") == position["symbol"]) if not frame.is_empty() else frame
            if reason is None and not history.is_empty():
                for row in history.iter_rows(named=True):
                    reason, _ = price_exit(policy, entry, peak, row.get("open"),
                                           row.get("low"), row.get("high"))
                    if reason:
                        bar = row["bar"]
                        break
                    if valid_price(row.get("high")):
                        peak = max(peak, row["high"]) if valid_price(peak) else row["high"]
            if reason is not None:
                hits.append({
                    "symbol": position["symbol"], "signals": ["risk_" + reason], "reason": reason,
                    "signal_bar": bar, "first_eligible_bar": bar + 1, "last_eligible_bar": 900,
                    "trigger_time": datetime.combine(context.as_of, datetime.min.time()).replace(
                        hour=bar // 60, minute=bar % 60).isoformat(),
                    "account_id": position.get("account_id"), "cycle_id": position.get("cycle_id"),
                })
        except (ValueError, TypeError, OSError, pl.exceptions.PolarsError) as exc:
            context.exit_errors.append(f"{position['symbol']}风控退出失败: {exc}")
    return hits


def optional_exit_hits(engine, strategy, context, params, definitions, *, overrides=None):
    """First selected exit wins; empty selection never invents a time guard."""
    from dataclasses import replace
    from datetime import datetime

    import polars as pl

    from app.market_time import CN_TZ, cn_now, cn_today
    from app.strategy.builtin._overnight import minute_frame
    from app.strategy.overnight_runtime import exit_hits
    from app.strategy.risk_policy import strategy_policy

    policy = strategy_policy(strategy, overrides or {})
    risk_enabled = any(value is not None for value in policy.to_dict().values())
    if not definitions and not risk_enabled:
        return []
    positions = [p for p in context.positions or [] if p.get("qty", 0) > 0
                 and p.get("available_qty", 0) > 0
                 and str(p.get("sell_date", "")) <= str(context.as_of)]
    if not positions:
        return []
    now = context.observed_at or context.completed_through
    if now is None:
        now = cn_now() if context.as_of == cn_today() else datetime.combine(
            context.as_of, datetime.min.time()).replace(hour=15)
    if now.tzinfo:
        now = now.astimezone(CN_TZ).replace(tzinfo=None)
    completed = context.completed_through
    if completed is not None:
        if completed.tzinfo:
            completed = completed.astimezone(CN_TZ).replace(tzinfo=None)
        cutoff = min(now, completed)
    else:
        cutoff = now
    frame = minute_frame(context.history) if context.history is not None else pl.DataFrame()
    if not frame.is_empty():
        frame = frame.filter((pl.col("datetime").dt.date() == context.as_of)
                             & (pl.col("datetime") <= cutoff))
    ctx = replace(context, history=frame, completed_through=now, positions=[
        {**p, "sell_date": str(context.as_of)} for p in positions])
    hits = risk_exit_hits(context, policy, positions, frame, now) if risk_enabled else []
    ordinary = [s for s in definitions if s.get("template") not in
                (TEMPLATES["exit"], "morning_liquidation")]
    minute_names = [s["column"] for s in ordinary if s.get("timeframe") == "intraday"]
    if minute_names and not frame.is_empty():
        frame = engine._inject_intraday_signal_columns(
            frame, context=ctx, requested_signals=set(minute_names))
    daily = daily_signal_frame(ctx, ordinary)
    for sig in definitions:
        column = sig["column"]
        if sig.get("template") == TEMPLATES["exit"]:
            hits.extend({**h, "signals": [column]} for h in exit_hits(
                ctx, {**params, **signal_parameters(sig)}))
        elif sig.get("template") == "morning_liquidation":
            from app.strategy.late_session_strength import previous_closes
            from app.strategy.morning_liquidation import evaluate
            evaluated = evaluate(
                ctx.history, sig, day=ctx.as_of, now=now, positions=positions,
                prev_close=previous_closes(ctx), quotes=ctx.current,
                suspension_history=ctx.suspension_history)
            context.exit_errors.extend(evaluated.diagnostics)
            for hit in evaluated.hits:
                stamp = datetime.fromisoformat(hit["trigger_time"])
                bar = stamp.hour * 60 + stamp.minute
                hits.append({**hit, "signals": [column], "signal_bar": bar,
                             "first_eligible_bar": bar + 1, "last_eligible_bar": 900})
        else:
            if sig.get("timeframe") == "intraday":
                if frame.is_empty():
                    continue
                if column not in frame.columns:
                    raise ValueError("S01触发器缺少信号数据: " + column)
                events = frame.filter(pl.col(column).fill_null(False)).group_by("symbol").head(1)
                clocks = {r["symbol"]: r["bar"] for r in events.to_dicts()}
            else:
                clocks = {r["symbol"]: 570 for r in daily.filter(pl.col(column).fill_null(False)).to_dicts()}
            for position in positions:
                bar = clocks.get(position["symbol"])
                if bar is not None and bar <= now.hour * 60 + now.minute:
                    hits.append({"symbol": position["symbol"], "signals": [column], "reason": "selected_signal",
                                 "signal_bar": bar, "first_eligible_bar": bar + 1, "last_eligible_bar": 900,
                                 "account_id": position.get("account_id"), "cycle_id": position.get("cycle_id")})
    earliest = {}
    for hit in sorted(hits, key=lambda h: (
            h["signal_bar"], 0 if any(s.startswith("risk_") for s in h["signals"]) else 1, h["symbol"])):
        key = (hit["symbol"], hit.get("account_id"), hit.get("cycle_id"))
        if key not in earliest:
            earliest[key] = hit
        elif hit["signal_bar"] == earliest[key]["signal_bar"]:
            earliest[key]["signals"].extend(s for s in hit["signals"] if s not in earliest[key]["signals"])
    return list(earliest.values())
