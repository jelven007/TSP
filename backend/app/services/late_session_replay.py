"""共享尾盘模板的本地信号回放, 使用策略保存的完整当日股票池。

回放不恢复S02调度器的独立阶段、修复历史或发布回执; 不模拟成交和收益。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import polars as pl

from app.services.daily_stock_pools import DailyStockPools
from app.strategy.late_session_strength import (
    condition_inputs,
    evaluate,
    evaluation_time,
    needs_previous_close,
    parameters,
)


def replay(repo, storage, sig, *, strategy_id, start, end, symbols):
    """在含首尾的日期区间计算模板触发, 返回扫描计数、triggers和diagnostics。

    每日按保存完整池计算, symbols只限制返回触发; 缺池记诊断并跳过该日。
    仅显式entry_groups读取资格快照。定时信号只评估保存确认点, 非定时
    信号逐完成分钟评估; 仓库或计算异常向上抛出, 不伪装扫描成功。
    """
    triggers, diagnostics = [], []
    days_scanned = bars_scanned = 0
    params = parameters(sig)
    day = start
    while day <= end:
        pool = DailyStockPools(storage).read(strategy_id, day, limit=100000)
        if pool is None:
            diagnostics.append({"date": str(day), "message": "缺少该策略当日股票池快照, 未回放"})
            day += timedelta(days=1)
            continue
        universe = pool["symbols"]
        selection = None
        if params.get("entry_groups") is not None:
            from app.services.daily_selections import DailySelections

            selection = DailySelections(storage).read(strategy_id, day)
        daily = pl.DataFrame()
        if needs_previous_close(sig) or params["volume_ratio_min"] is not None:
            load_prior = getattr(repo, "get_last_traded_closes", None)
            if params.get("entry_groups") is None and params["volume_ratio_min"] is None and callable(load_prior):
                daily = load_prior(universe, day)
            else:
                from app.strategy.basic_filter import previous_session

                earliest = day
                for _ in range(5 if params["volume_ratio_min"] is not None else 1):
                    earliest = previous_session(earliest)
                daily = repo.get_daily_batch(
                    universe, earliest, day-timedelta(days=1),
                    columns=["symbol", "date", "close", "raw_close", "volume"])
        previous = {}
        if not daily.is_empty():
            for row in daily.sort("date").iter_rows(named=True):
                previous[str(row["symbol"])] = row["raw_close"] if "raw_close" in row else row["close"]
        minute = repo.get_minute_batch(universe, day, asset_type="stock")
        if minute is not None and not minute.is_empty():
            days_scanned += 1
            bars_scanned += minute.height
        capital = None
        if params["turnover_min_pct"] is not None:
            # 换手率才读取证券股本并套用历史股本, 不从分钟价格推测分母。
            getter = getattr(repo, "get_instruments", None)
            if callable(getter):
                from app.market_time import cn_today
                from app.share_capital import apply_historical_float_shares

                capital = getter().with_columns(pl.lit(day).alias("date"))
                shares_getter = getattr(repo, "get_historical_shares", None)
                capital = apply_historical_float_shares(
                    capital, shares_getter() if callable(shares_getter) else None, today=cn_today())
        inputs = condition_inputs(SimpleNamespace(
            as_of=day, daily_history=daily, current=capital, pool_history=None, pool_universe=None), sig)
        scheduled = evaluation_time(sig, day)
        points = [scheduled] if scheduled is not None else []
        if scheduled is None and minute is not None and not minute.is_empty():
            points = [at + timedelta(minutes=params["evaluation_delay_minutes"] or 0)
                      for at in minute["datetime"].unique().sort().to_list()
                      if at.date() == day]
        for observed in points:
            point_frame = minute
            if scheduled is None:
                point_frame = minute.filter(pl.col("datetime") <= observed - timedelta(
                    minutes=params["evaluation_delay_minutes"] or 0))
            result = evaluate(point_frame, sig, day=day, pool_symbols=universe, prev_close=previous,
                              completed_through=observed, selection=selection, **inputs)
            diagnostics.extend({"date": str(day), "message": message} for message in result.diagnostics)
            label = datetime.fromisoformat(result.progress["bar_at"]) if result.progress["bar_at"] else None
            if label is None:
                continue
            available_at = evaluation_time(sig, day, completed_through=label)
            triggers.extend({"date": str(day), "time": str(available_at.time()), "symbol": symbol,
                             **result.entry_matches.get(symbol, {}),
                             **({"bar_time": str(label.time())} if available_at > label else {})}
                            for symbol in result.symbols if symbol in symbols)
        day += timedelta(days=1)
    return {
        "signal_id": sig["id"], "strategy_id": strategy_id,
        "start_date": str(start), "end_date": str(end), "symbols": symbols,
        "days_scanned": days_scanned, "bars_scanned": bars_scanned,
        "triggers": triggers, "diagnostics": diagnostics,
    }
