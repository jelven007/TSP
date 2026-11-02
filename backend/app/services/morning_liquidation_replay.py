"""用明确买入日期回放早盘清仓信号, 不模拟成交或收益。"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from app.market_time import published_trading_day
from app.strategy.morning_liquidation import evaluate, parameters


def replay(repo, sig, *, start, end, symbols, positions):
    suspension_history = getattr(repo, "get_suspension_history", lambda: None)()
    daily = repo.get_daily_batch(symbols, start-timedelta(days=15), end, columns=["symbol", "date", "close", "raw_close"])
    triggers, diagnostics = [], []
    params = parameters(sig)
    legacy = "amplitude_pct" in params
    bars_scanned = days_scanned = 0
    day = start
    while day <= end:
        minutes = repo.get_minute_batch(symbols, day, asset_type="stock")
        if minutes is not None and not minutes.is_empty():
            bars_scanned += minutes.height
            days_scanned += 1
        prior = daily.filter(daily["date"] < day).sort("date").unique("symbol", keep="last") if not daily.is_empty() else daily
        if not legacy:
            previous = day - timedelta(days=1)
            while published_trading_day(previous) is False:
                previous -= timedelta(days=1)
            prior = prior.filter(prior["date"] == previous) if not prior.is_empty() else prior
            if published_trading_day(previous) is not True:
                prior = prior.clear()
        references = {row["symbol"]: row.get("raw_close", row["close"]) for row in prior.iter_rows(named=True)}
        observations = [datetime.combine(day, time(15))]
        if not legacy:
            stop = datetime.combine(day, time.fromisoformat(params["time"]))
            observations = [stop - timedelta(microseconds=1), stop,
                            datetime.combine(day, time.fromisoformat(params["fallback_time"])),
                            datetime.combine(day, time(15))]
        seen = set()
        for at in observations:
            result = evaluate(minutes, sig, day=day, now=at,
                              positions=positions, prev_close=references, suspension_history=suspension_history)
            for hit in result.hits:
                key = (hit["symbol"], hit.get("account_id"), hit.get("cycle_id"), hit["entry_date"],
                       hit["reason"], hit["trigger_time"])
                if key not in seen:
                    triggers.append({"date": str(day), "time": hit["trigger_time"].split("T")[1], **hit})
                    seen.add(key)
            diagnostics.extend({"date": str(day), "message": message} for message in result.diagnostics)
        day += timedelta(days=1)
    return {"signal_id": sig["id"], "start_date": str(start), "end_date": str(end), "symbols": symbols,
            "days_scanned": days_scanned, "bars_scanned": bars_scanned, "triggers": triggers,
            "diagnostics": diagnostics, "execution": "signal_only"}
