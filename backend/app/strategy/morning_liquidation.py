"""早盘清仓与已确认停牌后的复牌卖出信号, 不执行委托。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, time

import polars as pl

from app.market_time import CN_TZ, published_trading_day
from app.strategy.overnight_runtime import next_market_day

TEMPLATE = "morning_liquidation"
DEFAULT_PARAMS = {"time": "10:30:00", "amplitude_pct": 6.0}


def parameters(sig: dict) -> dict:
    raw = sig.get("params") or {}
    if not isinstance(raw, dict) or set(raw) - set(DEFAULT_PARAMS):
        raise ValueError("早盘清仓参数格式错误或包含未知参数")
    params = {**DEFAULT_PARAMS, **raw}
    try:
        at = time.fromisoformat(params["time"])
        if at.strftime("%H:%M:%S") != params["time"] or at.tzinfo or not time(9, 30) <= at <= time(11, 30):
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("早盘清仓时间须为 09:30:00~11:30:00 (北京时间)") from None
    value = params["amplitude_pct"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError("振幅阈值须为 0~100% 的有限数值")
    return params


@dataclass
class Evaluation:
    hits: list[dict] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def evaluate(
    minute_df: pl.DataFrame, sig: dict, *, day: date, now: datetime,
    positions: list[dict] | None, prev_close: dict[str, float],
    quotes: pl.DataFrame | None = None,
    suspension_history: pl.DataFrame | None = None,
) -> Evaluation:
    params = parameters(sig)
    result = Evaluation()
    if sig.get("enabled") is False:
        return result
    now = now.astimezone(CN_TZ).replace(tzinfo=None) if now.tzinfo else now
    if now.date() != day or now.time() < time(9, 30):
        return result
    if positions is None:
        result.diagnostics.append("早盘清仓需要包含买入日期的实际持仓快照")
        return result
    deadline = datetime.combine(day, time.fromisoformat(params["time"]))
    # 已完成分钟仅提供触发时间上界; 缺口期间的真实首次触发不可伪造。
    frame = pl.DataFrame()
    if minute_df is not None and {"symbol", "datetime", "high", "low"}.issubset(minute_df.columns):
        dt = pl.col("datetime")
        if minute_df.schema["datetime"].time_zone:
            dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
        frame = minute_df.with_columns(dt.alias("datetime")).filter(
            (pl.col("datetime").dt.date() == day) & (pl.col("datetime").dt.time() > time(9, 30))
            & (pl.col("datetime") <= min(now, deadline))
            & pl.col("high").is_finite() & pl.col("low").is_finite()
            & (pl.col("low") > 0) & (pl.col("high") >= pl.col("low")),
        ).sort("datetime")
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
        if position.get("qty", 0) <= 0 or position.get("available_qty", 0) <= 0:
            continue
        from app.instrument_suspension import holding_exit_status, resume_exit_time

        status = holding_exit_status(suspension_history, symbol, sell_day, day, observed_at=now)
        if status == "suspended":
            result.diagnostics.append(f"早盘清仓: {symbol} 已确认停牌, 持有至复牌直接卖出")
            continue
        if sell_day != day and status != "resume":
            continue
        if status == "resume":
            result.hits.append({
                "symbol": symbol, "account_id": position.get("account_id"), "cycle_id": position.get("cycle_id"),
                "entry_date": str(entry), "available_qty": position["available_qty"], "holding_exit": True,
                "deadline_elapsed": False, "reason": "resume", "amplitude_pct": None,
                "trigger_time": resume_exit_time(suspension_history, symbol, day, observed_at=now).isoformat(),
                "source": "suspension_evidence",
            })
            continue
        pending = None
        previous = prev_close.get(symbol)
        history = minute_groups.get((symbol,))
        if _positive(previous) and history is not None:
            ranges = history.with_columns(
                ((pl.col("high").cum_max()-pl.col("low").cum_min()) / previous * 100).alias("_amplitude"),
            ).filter(pl.col("_amplitude") + 1e-10 >= params["amplitude_pct"])
            if not ranges.is_empty():
                row = ranges.row(0, named=True)
                pending = {"reason": "amplitude", "amplitude_pct": row["_amplitude"],
                           "trigger_time": row["datetime"].isoformat(), "source": "completed_minute"}
        quote = live.get(symbol, {})
        reference = quote.get("prev_close") if _positive(quote.get("prev_close")) else previous
        stamp = quote.get("quote_ts", quote.get("timestamp"))
        if (_positive(stamp) and _positive(reference) and _positive(quote.get("high"))
                and _positive(quote.get("low")) and quote["high"] >= quote["low"]
                and now < deadline and pending is None):
            seconds = stamp / 1000 if stamp > 1e12 else stamp
            at = datetime.fromtimestamp(seconds, CN_TZ).replace(tzinfo=None)
            if at.date() == day and at.time() >= time(9, 30) and -2 <= (now-at).total_seconds() <= 15:
                amplitude = (quote["high"]-quote["low"]) / reference * 100
                if amplitude + 1e-10 >= params["amplitude_pct"]:
                    pending = {"reason": "amplitude", "amplitude_pct": amplitude,
                               "trigger_time": at.isoformat(), "source": "live_quote"}
        if pending is None and now >= deadline:
            pending = {"reason": "deadline", "amplitude_pct": None,
                       "trigger_time": deadline.isoformat(), "source": "clock"}
        if pending:
            result.hits.append({
                "symbol": symbol, "account_id": position.get("account_id"), "cycle_id": position.get("cycle_id"),
                "entry_date": str(entry), "available_qty": position["available_qty"],
                "holding_exit": True,
                "deadline_elapsed": now > deadline, **pending,
            })
        elif not _positive(previous) or history is None:
            result.diagnostics.append(f"早盘清仓: {symbol} 缺少可用振幅行情或昨收, 截止时钟仍有效")
    return result


def selected_definitions(engine, names: list[str]) -> list[dict]:
    from app.strategy import custom_signals

    if not any(str(name).startswith("csgi_") for name in names):
        return []
    root = engine._user_data_dir()
    if root is None:
        return []
    return [sig for sig in custom_signals.load_all(root) if sig.get("template") == TEMPLATE
            and custom_signals.intraday_column_name(sig["id"]) in names]


def holding_snapshot(data_dir, strategy_id: str, as_of: date) -> list[dict]:
    """同一租户账本按剩余买入批次分日期, 不把加仓归入首笔日期。"""
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
                })
                row["qty"] += lot["qty"]
                row["available_qty"] += paper._available_of({"lots": [lot]}, str(as_of))
            result.extend(lots.values())
    return result
