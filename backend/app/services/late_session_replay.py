"""尾盘走强本地回放, 使用策略保存的完整当日股票池快照。"""
from datetime import timedelta

from app.services.daily_stock_pools import DailyStockPools
from app.strategy.late_session_strength import bar_time, evaluate, evaluation_time


def replay(repo, storage, sig, *, strategy_id, start, end, symbols):
    triggers, diagnostics = [], []
    days_scanned = bars_scanned = 0
    day = start
    while day <= end:
        pool = DailyStockPools(storage).read(strategy_id, day, limit=100000)
        if pool is None:
            diagnostics.append({"date": str(day), "message": "缺少该策略当日股票池快照, 未回放"})
            day += timedelta(days=1)
            continue
        universe = pool["symbols"]
        daily = repo.get_daily_batch(
            universe, day-timedelta(days=15), day-timedelta(days=1),
            columns=["symbol", "date", "close", "raw_close"])
        previous = {}
        if not daily.is_empty():
            for row in daily.sort("date").iter_rows(named=True):
                previous[str(row["symbol"])] = row.get("raw_close", row["close"])
        minute = repo.get_minute_batch(universe, day, asset_type="stock")
        if minute is not None and not minute.is_empty():
            days_scanned += 1
            bars_scanned += minute.height
        result = evaluate(minute, sig, day=day, pool_symbols=universe, prev_close=previous)
        diagnostics.extend({"date": str(day), "message": message} for message in result.diagnostics)
        available_at = evaluation_time(sig, day)
        label = bar_time(sig, day)
        triggers.extend({"date": str(day), "time": str(available_at.time()), "symbol": symbol,
                         **({"bar_time": str(label.time())} if available_at > label else {})}
                        for symbol in result.symbols if symbol in symbols)
        day += timedelta(days=1)
    return {
        "signal_id": sig["id"], "strategy_id": strategy_id,
        "start_date": str(start), "end_date": str(end), "symbols": symbols,
        "days_scanned": days_scanned, "bars_scanned": bars_scanned,
        "triggers": triggers, "diagnostics": diagnostics,
    }
