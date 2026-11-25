"""共享早盘出场内核: 按买入日期、保存信号及停复牌证据寻找首次触发。

信号关注名单和实仓分开输入, 返回hits/diagnostics; 外层负责任务冻结、
通知和成交。各策略当前启用参数仅在对应reference正文维护。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from itertools import pairwise

import polars as pl

from app.market_time import CN_TZ, published_trading_day
from app.strategy.overnight_runtime import next_market_day

TEMPLATE = "morning_liquidation"
# 共享模板默认值只用于相应输入契约; 实际策略参数读取所选保存信号。
# time是未提前触发时的时钟出场, fallback_time是停止卖出的截止。
DEFAULT_PARAMS = {"time": "10:30:00", "fallback_time": "10:45:00", "rise_pct": 6.0, "fall_pct": 6.0}
OPTIONAL_DEFAULTS = {"start_time": None, **DEFAULT_PARAMS, "decline_3m_pct": None, "volume_ratio_min": None}
BRACKET_DEFAULTS = {"take_profit_pct": None, "stop_loss_pct": None}


def parameters(sig: dict) -> dict:
    """解析已保存可空条件与历史冻结结构; null不恢复对应默认条件。

    time为时钟出场, fallback_time为停止边界; 成本阈值存百分点。
    共享模板默认值不代表某策略当前保存值, 校验失败交任务层记录。
    """
    raw = sig.get("params") or {}
    allowed = set(OPTIONAL_DEFAULTS) | set(BRACKET_DEFAULTS)
    if not isinstance(raw, dict) or set(raw) - (allowed | {"amplitude_pct"}):
        raise ValueError("早盘清仓参数格式错误或包含未知参数")
    legacy = "amplitude_pct" in raw
    if legacy and set(raw) & (allowed - {"time"}):
        raise ValueError("旧振幅与新涨跌幅参数不能混用")
    optional = not legacy and (bool(set(raw) - set(DEFAULT_PARAMS)) or any(v is None for v in raw.values()))
    if optional:
        params = {**OPTIONAL_DEFAULTS, **(BRACKET_DEFAULTS if set(raw) & set(BRACKET_DEFAULTS) else {}), **raw}
        for key in ("start_time", "time", "fallback_time"):
            value = params[key]
            if value is None:
                continue
            try:
                at = time.fromisoformat(value)
                if at.strftime("%H:%M:%S") != value or at.tzinfo:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("清仓时间须为 00:00:00~23:59:59 的 HH:MM:SS (北京时间)") from None
        start, stop, fallback = (params[k] for k in ("start_time", "time", "fallback_time"))
        if start is not None and any(at is not None and at < start for at in (stop, fallback)):
            raise ValueError("启动时间不得晚于止损或兜底时间")
        if stop is not None and fallback is not None and fallback <= stop:
            raise ValueError("兜底时间须晚于止损时间")
        for key in ("rise_pct", "fall_pct", "decline_3m_pct", "volume_ratio_min", *BRACKET_DEFAULTS):
            value = params.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not math.isfinite(value) or not .1 <= value <= 100):
                raise ValueError("数值参数须为0.1~100.0的有限数值")
        return params
    params = {**({"time": "10:30:00", "amplitude_pct": 6.0} if legacy else DEFAULT_PARAMS), **raw}
    for key in ("time",) if legacy else ("time", "fallback_time"):
        try:
            at = time.fromisoformat(params[key])
            if at.strftime("%H:%M:%S") != params[key] or at.tzinfo:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("清仓时间须为 00:00:00~23:59:59 的 HH:MM:SS (北京时间)") from None
    if not legacy and params["fallback_time"] <= params["time"]:
        raise ValueError("兜底时间须晚于止损时间")
    for key in ("amplitude_pct",) if legacy else ("rise_pct", "fall_pct"):
        value = params[key]
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                or not (0 <= value <= 100 if legacy else 0 < value <= 100)):
            raise ValueError("振幅阈值须为 0~100% 的有限数值" if legacy else "涨跌幅阈值须为大于0且不超过100%的有限数值")
    return params


def requires_previous_close(sig: dict) -> bool:
    """仅昨收涨跌幅或冻结振幅条件依赖昨收; 成本条件使用独立买入参考价。"""
    params = parameters(sig)
    return "amplitude_pct" in params or any(params.get(key) is not None for key in ("rise_pct", "fall_pct"))


@dataclass
class Evaluation:
    hits: list[dict] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _return_hit(price, previous, params):
    """相对精确原始昨收判断涨跌幅; 参数为百分点, 输出change_pct为小数。"""
    if not _positive(previous) or not _positive(price):
        return None
    change = price / previous - 1
    for key, passed, reason in (
        ("rise_pct", change, "rise"), ("fall_pct", -change, "fall"),
    ):
        if params[key] is not None and passed + 1e-12 >= params[key] / 100:
            return {"reason": reason, "change_pct": change}
    return None


def _holding_return_hit(price, entry_reference, params):
    """独立成本收益条件, 使用实仓成交均价或已标明口径的冻结买信号参考价。"""
    if entry_reference is None or not _positive(price):
        return None
    entry_price, basis = entry_reference
    pnl = price / entry_price - 1
    for key, passed, reason in (
        ("take_profit_pct", pnl, "take_profit"), ("stop_loss_pct", -pnl, "stop_loss"),
    ):
        if params.get(key) is not None and passed + 1e-12 >= params[key] / 100:
            return {"reason": reason, "pnl_pct": pnl, "entry_price": entry_price, "entry_price_basis": basis}
    return None


def _optional_minute_hit(history, price_field, previous, params, start, entry_reference=None):
    """按完成分钟前缀寻找首次触发, 未启用条件不要求专用窗口。

    同时启用跌幅和放量时需 13 根相邻 60 秒分钟: 末 4 根价格算 3 分钟跌幅,
    末 3 根均量除此前 10 根均量。无效值、零基准均量或缺口只跳过该窗口;
    start限制触发时刻, 允许其之前的回看数据, 不能跨午休。当前S03只启用
    成本止盈/止损, 沿每条完成分钟收盘寻找首次触发, 不要求连续13根窗口。
    """
    if history is None:
        return None
    rows = history.to_dicts()
    decline_enabled = params["decline_3m_pct"] is not None
    volume_enabled = params["volume_ratio_min"] is not None
    for index, row in enumerate(rows):
        at = row["datetime"]
        if at < start:
            continue
        pending = (_holding_return_hit(row.get(price_field), entry_reference, params)
                   or _return_hit(row.get(price_field), previous, params))
        if pending:
            return {**pending, "trigger_time": at.isoformat(), "source": "completed_minute"}
        if not (decline_enabled or volume_enabled):
            continue
        size = 13 if volume_enabled else 4
        if index + 1 < size:
            continue
        recent = rows[index + 1 - size:index + 1]
        if any((b["datetime"] - a["datetime"]).total_seconds() != 60
               for a, b in pairwise(recent)):
            continue
        metrics = {}
        if decline_enabled:
            prices = [r.get(price_field) for r in recent[-4:]]
            if not all(_positive(p) for p in prices):
                continue
            decline = (1 - prices[-1] / prices[0]) * 100
            if decline + 1e-10 < params["decline_3m_pct"]:
                continue
            metrics["decline_3m_pct"] = decline
        if volume_enabled:
            volumes = [r.get("volume") for r in recent]
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) and v >= 0 for v in volumes):
                continue
            prior_mean = sum(volumes[:10]) / 10
            if prior_mean <= 0:
                continue
            ratio = (sum(volumes[-3:]) / 3) / prior_mean
            if ratio + 1e-12 < params["volume_ratio_min"]:
                continue
            metrics["volume_ratio"] = ratio
        return {"reason": "volume_decline" if decline_enabled and volume_enabled else (
            "decline_3m" if decline_enabled else "volume_ratio"),
            **metrics, "trigger_time": at.isoformat(), "source": "completed_minute"}
    return None


def evaluate(
    minute_df: pl.DataFrame, sig: dict, *, day: date, now: datetime,
    positions: list[dict] | None, prev_close: dict[str, float],
    quotes: pl.DataFrame | None = None,
    suspension_history: pl.DataFrame | None = None,
    roster: list[dict] | None = None,
) -> Evaluation:
    """产生下一交易日或确认复牌的出场信号, 不写持仓/委托。

    roster 是前日成功买信号名单; positions 是实际可卖持仓, 两者分别处理。
    逐股日期错误保留诊断; 分钟缺失不关闭保存的时钟出场。fallback_time
    是停止边界, 截止后可返回首次触发供诊断, 但 deadline_elapsed 禁止重开卖出。
    止盈/止损用不含费买入均价, 信号名单用冻结原价; 缺成本不回退昨收。
    包括S02/S03在内的调用均沿各自保存信号, 不在共享模块补入未启用条件。
    同股分钟去重取最后一条; 先完成分钟、再新鲜报价、最后时钟。成本或
    分钟缺失保留诊断但不关闭时钟; 停复牌仅按可见证据改变持有周期。
    诊断与有效hits可同时返回, 外层保存首次命中并重试其余成员。
    """
    params = parameters(sig)
    result = Evaluation()
    optional = "start_time" in params
    if sig.get("enabled") is False or (optional and all(v is None for v in params.values())):
        return result
    now = now.astimezone(CN_TZ).replace(tzinfo=None) if now.tzinfo else now
    legacy = "amplitude_pct" in params
    if now.date() != day:
        return result
    # 信号页使用前日成功买名单, 实仓路径另校验可卖量; 两者均保留原买入日期。
    if roster is not None:
        positions = roster
    if positions is None:
        result.diagnostics.append("早盘清仓需要包含买入日期的实际持仓快照")
        return result
    deadline = datetime.combine(day, time.fromisoformat(params["time"])) if params["time"] is not None else None
    fallback_value = params.get("fallback_time", params["time"])
    fallback = datetime.combine(day, time.fromisoformat(fallback_value)) if fallback_value is not None else None
    start = datetime.combine(day, time.fromisoformat(params.get("start_time") or "00:00:00"))
    if optional and now < start:
        return result
    early_end = min([datetime.combine(day, time(23, 59, 59)),
                     *[at for at in (deadline, fallback) if at is not None]])
    # 已完成分钟仅提供触发时间上界; 缺口期间的真实首次触发不可伪造。
    frame = pl.DataFrame()
    price_field = "raw_close" if minute_df is not None and "raw_close" in minute_df.columns else "close"
    required = {"symbol", "datetime"} if optional else (
        {"symbol", "datetime", "high", "low"} if legacy else {"symbol", "datetime", price_field})
    if minute_df is not None and required.issubset(minute_df.columns):
        dt = pl.col("datetime")
        if minute_df.schema["datetime"].time_zone:
            dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
        valid = pl.lit(True) if optional else (pl.col("high").is_finite() & pl.col("low").is_finite()
                 & (pl.col("low") > 0) & (pl.col("high") >= pl.col("low"))) if legacy else (
            pl.col(price_field).is_finite() & (pl.col(price_field) > 0))
        frame = minute_df.with_columns(dt.alias("datetime")).filter(
            (pl.col("datetime").dt.date() == day) & (pl.col("datetime").dt.time() > time(9, 30))
            & (pl.col("datetime") <= min(now, early_end)) & valid
            & (pl.lit(True) if legacy or fallback is None else pl.col("datetime") < fallback),
        ).sort("datetime")
        if optional:
            frame = frame.filter(
                (pl.col("datetime").dt.second() == 0) & (pl.col("datetime").dt.microsecond() == 0),
            ).unique(["symbol", "datetime"], keep="last", maintain_order=True).sort("datetime")
    minute_groups = frame.partition_by("symbol", as_dict=True) if not frame.is_empty() else {}
    live = {}
    if quotes is not None and "symbol" in quotes.columns:
        live = {row["symbol"]: row for row in quotes.iter_rows(named=True)}
    for position in positions:
        symbol = str(position.get("symbol", ""))
        try:
            entry = date.fromisoformat(str(position.get("entry_date", "")))
            if published_trading_day(entry) is not True:
                raise ValueError("买入日期不是已确认交易日")
            sell_day = next_market_day(entry)
        except (TypeError, ValueError):
            result.diagnostics.append(f"早盘清仓: {symbol} 缺少有效买入日期或交易日历")
            continue
        if roster is None and (position.get("qty", 0) <= 0 or position.get("available_qty", 0) <= 0):
            continue
        entry_reference = None
        if any(params.get(key) is not None for key in BRACKET_DEFAULTS):
            cost = (position.get("raw_close", position.get("close")) if roster is not None
                    else position.get("entry_price"))
            if _positive(cost):
                entry_reference = (cost, "buy_signal_reference" if roster is not None else "filled_average")
            else:
                result.diagnostics.append(f"早盘清仓: {symbol} 缺少有效买入参考价, 已启用止盈止损不可确认")
        holding_fields = ({"signal_roster": True} if roster is not None
                          else {"available_qty": position["available_qty"]})
        from app.instrument_suspension import holding_exit_status, resume_exit_time

        status = holding_exit_status(suspension_history, symbol, sell_day, day, observed_at=now)
        # 只有已确认停复牌证据改变持有周期; 缺分钟不能据此猜测停牌或复牌。
        if status == "suspended":
            result.diagnostics.append(f"早盘清仓: {symbol} 已确认停牌, 持有至复牌直接卖出")
            continue
        if sell_day != day and status != "resume":
            continue
        if status == "resume":
            result.hits.append({
                "symbol": symbol, "account_id": position.get("account_id"), "cycle_id": position.get("cycle_id"),
                "entry_date": str(entry), **holding_fields, "holding_exit": True,
                "deadline_elapsed": False, "reason": "resume", "amplitude_pct": None,
                "trigger_time": resume_exit_time(suspension_history, symbol, day, observed_at=now).isoformat(),
                "source": "suspension_evidence",
            })
            continue
        # 先查已完成分钟的首次价格触发, 再查新鲜实时报价, 最后使用保存时钟。
        # 首次触发的持久化去重由外层任务负责, 本内核不执行委托或写账本。
        pending = None
        previous = prev_close.get(symbol)
        history = minute_groups.get((symbol,))
        if optional:
            pending = _optional_minute_hit(history, price_field, previous, params, start, entry_reference)
        elif legacy and _positive(previous) and history is not None:
            ranges = history.with_columns(
                ((pl.col("high").cum_max()-pl.col("low").cum_min()) / previous * 100).alias("_amplitude"),
            ).filter(pl.col("_amplitude") + 1e-10 >= params["amplitude_pct"])
            if not ranges.is_empty():
                row = ranges.row(0, named=True)
                pending = {"reason": "amplitude", "amplitude_pct": row["_amplitude"],
                           "trigger_time": row["datetime"].isoformat(), "source": "completed_minute"}
        elif _positive(previous) and history is not None:
            changes = history.with_columns((pl.col(price_field) / previous - 1).alias("_change")).filter(
                (pl.col("_change") + 1e-12 >= params["rise_pct"] / 100)
                | (pl.col("_change") - 1e-12 <= -params["fall_pct"] / 100))
            if not changes.is_empty():
                row = changes.row(0, named=True)
                pending = {"reason": "rise" if row["_change"] > 0 else "fall",
                           "change_pct": row["_change"],
                           "trigger_time": row["datetime"].isoformat(), "source": "completed_minute"}
        quote = live.get(symbol, {})
        reference = quote.get("prev_close") if _positive(quote.get("prev_close")) else previous
        stamp = quote.get("quote_ts", quote.get("timestamp"))
        if (legacy and _positive(stamp) and _positive(reference) and _positive(quote.get("high"))
                and _positive(quote.get("low")) and quote["high"] >= quote["low"]
                and now < deadline and pending is None):
            seconds = stamp / 1000 if stamp > 1e12 else stamp
            at = datetime.fromtimestamp(seconds, CN_TZ).replace(tzinfo=None)
            if at.date() == day and at.time() >= time(9, 30) and -2 <= (now-at).total_seconds() <= 15:
                amplitude = (quote["high"]-quote["low"]) / reference * 100
                if amplitude + 1e-10 >= params["amplitude_pct"]:
                    pending = {"reason": "amplitude", "amplitude_pct": amplitude,
                               "trigger_time": at.isoformat(), "source": "live_quote"}
        price = quote.get("price", quote.get("close"))
        if (not legacy and pending is None and now < early_end and _positive(stamp)
                and _positive(price)):
            seconds = stamp / 1000 if stamp > 1e12 else stamp
            at = datetime.fromtimestamp(seconds, CN_TZ).replace(tzinfo=None)
            if at.date() == day and start <= at < early_end and -2 <= (now-at).total_seconds() <= 15:
                hit = (_holding_return_hit(price, entry_reference, params)
                       or _return_hit(price, previous, params))
                if hit:
                    pending = {**hit,
                               "trigger_time": at.isoformat(), "source": "live_quote"}
        if (not legacy and pending is None and deadline is not None and now >= deadline
                and (fallback is None or deadline < fallback)):
            pending = {"reason": "stop_time", "change_pct": None,
                       "trigger_time": deadline.isoformat(), "source": "clock"}
        if legacy and pending is None and deadline is not None and now >= deadline:
            pending = {"reason": "deadline" if legacy else "stop_time",
                       "amplitude_pct" if legacy else "change_pct": None,
                       "trigger_time": deadline.isoformat(), "source": "clock"}
        if pending:
            result.hits.append({
                "symbol": symbol, "account_id": position.get("account_id"), "cycle_id": position.get("cycle_id"),
                "entry_date": str(entry), **holding_fields,
                "holding_exit": True,
                "deadline_elapsed": fallback is not None and (now > fallback if legacy else now >= fallback),
                **pending,
            })
        elif optional:
            if ((params["rise_pct"] is not None or params["fall_pct"] is not None) and not _positive(previous)):
                result.diagnostics.append(f"早盘清仓: {symbol} 缺少原始昨收, 已启用涨跌幅条件不可确认")
            if history is None and (params["decline_3m_pct"] is not None or params["volume_ratio_min"] is not None):
                result.diagnostics.append(f"早盘清仓: {symbol} 缺少放量下跌条件所需的完成分钟")
        elif not _positive(previous) or history is None:
            result.diagnostics.append(f"早盘清仓: {symbol} 缺少可用{'振幅' if legacy else '现价'}行情或昨收, 清仓时钟仍有效")
    return result


def selected_definitions(engine, names: list[str]) -> list[dict]:
    """读取已选早盘模板定义; 信号是否启用及参数有效性由执行入口继续校验。"""
    from app.strategy import custom_signals

    if not any(str(name).startswith("csgi_") for name in names):
        return []
    root = engine._user_data_dir()
    if root is None:
        return []
    return [sig for sig in custom_signals.load_all(root) if sig.get("template") == TEMPLATE
            and custom_signals.intraday_column_name(sig["id"]) in names]


def holding_snapshot(data_dir, strategy_id: str, as_of: date) -> list[dict]:
    """从同租户成交账本重放剩余FIFO批次, 按买入日期分别汇总数量与均价。

    不含交易费用; 任一剩余批次缺有效成交成本, 该日期成本保持未知。
    可卖量按as_of和T+1计算, 不把后来加仓归入首笔买入日期。
    """
    from app.strategy import paper

    result = []
    for account in paper.list_account_ids(data_dir):
        fills = [row for row in paper.load_fills(data_dir, account) if row.get("date", "") <= str(as_of)]
        positions, _cash = paper.replay_positions(data_dir, fills, account)
        for symbol, position in positions.items():
            if (position.get("entry_policy") or {}).get("strategy_id") != strategy_id:
                continue
            lots = {}
            for lot in position.get("lots", []):
                if lot["qty"] <= 0:
                    continue
                entry = lot.get("date", "")
                row = lots.setdefault(entry, {
                    "symbol": symbol, "entry_date": entry, "account_id": account,
                    "cycle_id": position.get("cycle_id"), "qty": 0, "available_qty": 0,
                    "_entry_notional": 0., "_cost_complete": True,
                })
                row["qty"] += lot["qty"]
                row["available_qty"] += paper._available_of({"lots": [lot]}, str(as_of))
                if _positive(lot.get("entry_price")):
                    row["_entry_notional"] += lot["entry_price"] * lot["qty"]
                else:
                    row["_cost_complete"] = False
            for row in lots.values():
                notional, complete = row.pop("_entry_notional"), row.pop("_cost_complete")
                row["entry_price"] = notional / row["qty"] if complete else None
                result.append(row)
    return result
