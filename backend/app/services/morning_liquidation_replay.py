"""用明确买入日期回放早盘清仓信号, 不模拟成交或收益。"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from app.strategy.morning_liquidation import evaluate


def replay(repo, sig, *, start, end, symbols, positions):
    daily = repo.get_daily_batch(symbols, start-timedelta(days=15), end, columns=["symbol", "date", "close", "raw_close"])
    triggers, diagnostics = [], []
    bars_scanned = days_scanned = 0
    day = start
    while day <= end:
        minutes = repo.get_minute_batch(symbols, day, asset_type="stock")
        if minutes is not None and not minutes.is_empty():
            bars_scanned += minutes.height
            days_scanned += 1
        prior = daily.filter(daily["date"] < day).sort("date").unique("symbol", keep="last") if not daily.is_empty() else daily
        references = {row["symbol"]: row.get("raw_close", row["close"]) for row in prior.iter_rows(named=True)}
        result = evaluate(minutes, sig, day=day, now=datetime.combine(day, time(11, 30)),
                          positions=positions, prev_close=references)
        for hit in result.hits:
            triggers.append({"date": str(day), "time": hit["trigger_time"].split("T")[1], **hit})
        diagnostics.extend({"date": str(day), "message": message} for message in result.diagnostics)
        day += timedelta(days=1)
    return {"signal_id": sig["id"], "start_date": str(start), "end_date": str(end), "symbols": symbols,
            "days_scanned": days_scanned, "bars_scanned": bars_scanned, "triggers": triggers,
            "diagnostics": diagnostics, "execution": "signal_only"}
