"""分钟持仓上下文适配。只读现有模拟账本, 不创建委托。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from app.market_time import cn_now, cn_today, published_trading_day
from app.strategy.builtin._overnight import exit_signal, minute_frame, minute_number, parameters


def next_market_day(day: date) -> date:
    for offset in range(1, 15):
        candidate = day + timedelta(days=offset)
        known = published_trading_day(candidate)
        if known is None:
            raise ValueError("隔夜策略需要有证据的交易日历")
        if known:
            return candidate
    raise ValueError("无法确定次个交易日")


def annotate_daily(frame: pl.DataFrame, repo) -> pl.DataFrame:
    """冻结目录与已记录风险警示的并集, 排除已记录前日除权事件。"""
    if frame.is_empty():
        return frame
    from app.strategy.eligibility import attach_listing_age

    instruments = repo.get_instruments_asset("stock")
    frame = attach_listing_age(frame, instruments)
    warnings = set()
    for source in (instruments, getattr(repo, "get_instrument_history", lambda: pl.DataFrame())()):
        if source is None or source.is_empty():
            continue
        if "is_risk_warning" in source.columns:
            warnings.update(source.filter(pl.col("is_risk_warning").fill_null(False))["symbol"].to_list())
        if "name" in source.columns:
            warnings.update(source.filter(pl.col("name").fill_null("").str.to_uppercase().str.contains("ST"))["symbol"].to_list())
    previous = pl.col("is_risk_warning").fill_null(False) if "is_risk_warning" in frame.columns else pl.lit(False)
    frame = frame.with_columns((previous | pl.col("symbol").is_in(sorted(warnings))).alias("is_risk_warning"))
    data_dir = getattr(getattr(repo, "store", None), "data_dir", None)
    if data_dir is not None:
        from app.services.corporate_actions import load_actions

        events = load_actions(data_dir)
        if events:
            excluded = pl.DataFrame({
                "symbol": [e.symbol for e in events], "date": [e.ex_date for e in events],
                "prior_ex_date": [True] * len(events),
            }).unique(["symbol", "date"])
            frame = frame.drop("prior_ex_date", strict=False).join(excluded, on=["symbol", "date"], how="left")
    return frame


def monitor_context(loader, engine, strategy_id, as_of, current, data_dir):
    service = getattr(loader, "__self__", None)
    if service is None or not hasattr(service, "build_strategy_context"):
        raise ValueError("分钟监控缺少共享行情仓库")
    capacity = engine.get(strategy_id).meta.get("minute_execution") == "overnight_capacity_v1"
    try:
        context = service.build_strategy_context(
            engine, as_of, [strategy_id], timeframe="1m", current=current)
    except ValueError:
        if not capacity:
            raise
        # 缺分钟数据仍允许时钟清仓, 指标分支保持关闭。
        from app.strategy.engine import StrategyDataContext

        context = StrategyDataContext("stock", "1m", as_of, current=current, history=pl.DataFrame(),
                                      completed_through=cn_now())
    if capacity:
        context.positions = position_snapshot(data_dir, strategy_id, as_of) if data_dir is not None else []
    if engine.get(strategy_id).meta.get("minute_execution") == "momentum_capacity_v1":
        from app.services.strategy_results import previous_daily_state

        loader = getattr(engine, "_daily_state_loader", None)
        if callable(loader):
            previous = loader(strategy_id, as_of)
        elif data_dir is not None:
            previous = previous_daily_state(data_dir, strategy_id, as_of)
        else:
            raise ValueError("连续策略组合缺少持久化状态目录")
        context.previous_states = {strategy_id: previous}
    return context


def position_snapshot(data_dir, strategy_id: str, as_of: date | None = None) -> list[dict]:
    """按账户保留成本与周期; 只读取归属于该策略的已成交持仓。"""
    from app.strategy import paper

    positions = []
    as_of = as_of or cn_today()
    for account in paper.list_account_ids(data_dir):
        fills = [f for f in paper.load_fills(data_dir, account) if f.get("date", "") <= str(as_of)]
        for symbol, pos in paper.replay_positions(data_dir, fills, account)[0].items():
            policy = pos.get("entry_policy") or {}
            if pos.get("qty", 0) <= 0 or policy.get("strategy_id") != strategy_id:
                continue
            entry = date.fromisoformat(pos["entry_date"][:10])
            positions.append({
                **pos,
                "symbol": symbol, "account_id": account, "cycle_id": pos.get("cycle_id"),
                "qty": pos["qty"], "available_qty": paper._available_of(pos, str(as_of)),
                "entry_unit_cost": pos["avg_cost"], "entry_date": str(entry),
                "sell_date": str(next_market_day(entry)),
                "day_sell_notional": pos.get("sell_days", {}).get(str(as_of), {}).get("notional", 0.),
            })
    return positions


def exit_hits(context, params: dict) -> list[dict]:
    deadline = minute_number(parameters(params)["sell_deadline"])
    frame = minute_frame(context.history if context.history is not None else pl.DataFrame())
    now = context.completed_through
    if now is None:
        now = (cn_now().replace(tzinfo=None) if context.as_of == cn_today()
               else datetime.combine(context.as_of, datetime.min.time()).replace(hour=15))
    now = now.replace(second=0, microsecond=0)
    if now.tzinfo is not None:
        from app.market_time import CN_TZ
        now = now.astimezone(CN_TZ).replace(tzinfo=None)
    asof = now.hour * 60 + now.minute
    if not frame.is_empty():
        frame = frame.filter((pl.col("datetime").dt.date() == context.as_of)
                             & (pl.col("datetime") <= now))
    hits = []
    for position in context.positions or []:
        if (str(position.get("sell_date")) != str(context.as_of)
            or position.get("qty", 0) <= 0 or position.get("available_qty", 0) <= 0):
            continue
        symbol = position["symbol"]
        history = frame.filter(pl.col("symbol") == symbol) if not frame.is_empty() else frame
        pending = None
        # 回放已完成前缀的首次触发, 后续反弹不撤销; 指标不足仍有时钟兜底。
        for bar in range(675, min(asof, deadline) + 1):
            pending = exit_signal(history, bar, position.get("entry_unit_cost"), params, pending)
            if pending:
                break
        if pending:
            hits.append({
                "symbol": symbol, "signals": ["overnight_" + pending["reason"]],
                "account_id": position.get("account_id"), "cycle_id": position.get("cycle_id"),
                **pending, "deadline_elapsed": asof > deadline,
            })
    return hits
