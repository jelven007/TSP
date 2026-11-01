"""连续研究组合的单日卖、选、买; 不写模拟账户或真实交易账户。"""
from __future__ import annotations

import copy
import math

import polars as pl

from app.backtest.momentum import sell_position
from app.backtest.overnight import fill_order
from app.strategy.builtin._overnight import minute_frame, minute_number, suspended_on
from app.strategy.momentum_rules import parameters, stop_quote, target_quote
from app.strategy.momentum_selection import prioritized_selection, ranked_candidates
from app.strategy.overnight_runtime import next_market_day


def run_day(context, values, selected, *, previous_state=None, allow_buy=True):
    evidence = {k: previous_state[k] for k in ("suspension_policy", "suspension_events")
                if previous_state and k in previous_state}
    params = parameters({**evidence, **(values or {})})
    state = copy.deepcopy(previous_state or {"cash": params["initial_cash"], "positions": []})
    cash = float(state["cash"])
    positions = {p["symbol"]: p for p in state["positions"]}
    frame = minute_frame(context.history if context.history is not None else pl.DataFrame())
    day = context.as_of
    if not frame.is_empty():
        frame = frame.filter(pl.col("datetime").dt.date() == day)
        if context.completed_through is not None:
            frame = frame.filter(pl.col("datetime") <= context.completed_through.replace(tzinfo=None))
    last_bar = (context.completed_through.hour * 60 + context.completed_through.minute
                if context.completed_through is not None else 900)
    daily = context.daily_history if context.daily_history is not None else pl.DataFrame()
    prior = daily.filter(pl.col("date") < day).sort("symbol", "date") if not daily.is_empty() else daily
    lookup = ({r["symbol"]: r.get("raw_close", r.get("close"))
               for r in prior.group_by("symbol", maintain_order=True).tail(1).to_dicts()}
              if not prior.is_empty() else {})
    sections = {"sell": [], "select": prioritized_selection(selected, params).to_dicts(), "buy": []}
    fills, trades, signals, decisions, suspensions = [], [], [], [], []
    error = None

    def finish():
        nonlocal error
        nav, frozen = cash, 0.
        for symbol, pos in positions.items():
            known = frame.filter(pl.col("symbol") == symbol) if not frame.is_empty() else frame
            mark = known["close"][-1] if not known.is_empty() else pos.get("last_mark")
            if mark is None or not math.isfinite(mark) or mark <= 0:
                error = error or f"{day} {symbol}缺少原始估值"
                continue
            if known.is_empty() and not pos.get("suspended") and last_bar >= 900:
                error = error or f"{day} {symbol}缺少收盘分钟估值"
            pos["last_mark"] = mark
            nav += pos["remaining"] * mark
            if pos.get("suspended"):
                frozen += pos["remaining"] * mark
        return {
            "as_of": str(day), "rows": sections["select"], "total": len(sections["select"]),
            "sections": sections, "state": {"as_of": str(day), "cash": cash,
                "positions": list(positions.values()), "nav": nav, "complete": error is None and last_bar >= 900,
                **{k: params[k] for k in ("suspension_policy", "suspension_events") if k in params}},
            "fills": fills, "trades": trades, "signals": signals, "entry_decisions": decisions,
            "suspensions": suspensions, "error": error,
            "equity": {"date": str(day), "value": nav, "cash": cash, "positions": len(positions),
                       "frozen_market_value": frozen},
            "portfolio_scope": "strategy_research_simulation",
        }

    # 先执行14:32及以前卖出, 买入后再继续; 不提前使用下午卖出现金或空位。
    sell_symbols = list(positions)
    sell_rows = {}

    def execute_sales(first_bar, last):
      nonlocal cash, error
      for symbol in sell_symbols:
        if symbol not in positions:
            continue
        pos = positions[symbol]
        if str(day) < str(pos["sell_date"]):
            continue
        row = sell_rows.get(symbol)
        if row is None:
            row = {"symbol": symbol, "name": pos.get("name"), "shares": pos["remaining"], "entry_price": pos["entry_notional"] / pos["shares"],
               "entry_value": pos["entry_value"], "net_stop_quote": stop_quote(pos, params),
               "gross_target_quote": target_quote(pos, params), "guard_time": params["guard_time"],
               "sell_deadline": params["sell_deadline"], "sell_date": pos["sell_date"]}
            sell_rows[symbol] = row
            sections["sell"].append(row)
        if suspended_on(symbol, day, params):
            pos["suspended"] = True
            row.update(reason="suspended", remaining=pos["remaining"], status="停牌等待复牌")
            if first_bar == 571:
                suspensions.append({"date": str(day), "symbol": symbol, "remaining": pos["remaining"]})
            continue
        if pos.pop("suspended", False):
            pos["time_guard_pending"] = True
        source = frame.filter(pl.col("symbol") == symbol) if not frame.is_empty() else frame
        deadline = minute_number(params["sell_deadline"])
        last = min(last, deadline)
        expected = {b for b in range(first_bar, last + 1) if b <= 690 or b >= 781}
        actual = set(source["bar"].to_list()) if not source.is_empty() else set()
        if expected - actual:
            error = f"{day} {symbol}卖出窗口缺少{len(expected - actual)}根分钟, 保留实际持仓"
            row.update(status="数据不足", remaining=pos["remaining"])
            return
        source = source.filter(pl.col("bar").is_between(first_bar, last)) if not source.is_empty() else source
        sale = sell_position(source, symbol, lookup.get(symbol), pos, params)
        fills.extend(sale["fills"])
        signals.extend({"date": str(day), "symbol": symbol, **s} for s in sale["signals"])
        cash += sale["value"]
        pos["remaining"] = sale["remaining"]
        pos["sell_notional"] = pos.get("sell_notional", 0.) + sale["notional"]
        pos["sell_fee_paid"] = pos.get("sell_fee_paid", 0.) + sale["fee"]
        pos["stop_pending"] = sale["stop_pending"]
        pos["day_sell_notional"] = sale["day_sell_notional"]
        pos["sell_fee_date"] = sale["sell_fee_date"]
        reason = row.get("reason", "pending")
        if reason == "pending" and sale["signals"]:
            reason = sale["signals"][0]["reason"]
        row.update(reason=reason,
                   sold_shares=row.get("sold_shares", 0) + sale["shares"],
                   remaining=pos["remaining"], sell_fee=row.get("sell_fee", 0.) + sale["fee"],
                   sell_notional=row.get("sell_notional", 0.) + sale["notional"],
                   status="已完成" if not pos["remaining"] else "等待成交")
        if pos["remaining"]:
            if last >= deadline:
                error = f"{day} {symbol}{params['sell_deadline']}到期剩余{pos['remaining']}股, 保留实际持仓"
                row["status"] = "到期未完成"
                return
            continue
        exit_value = pos["sell_notional"] - pos["sell_fee_paid"]
        from app.market_time import published_trading_days
        entry_day = pos["entry_date"]
        holding_sessions = sum(str(entry_day) < str(d) <= str(day)
                               for year in range(int(str(entry_day)[:4]), day.year + 1)
                               for d in published_trading_days(year))
        trade = {
            "symbol": symbol, "entry_date": pos["entry_date"] + " " + pos.get("last_buy_time", "14:32"),
            "exit_date": str(day) + " " + sale["fills"][-1]["datetime"][11:16],
            "entry_price": pos["entry_notional"] / pos["shares"], "exit_price": pos["sell_notional"] / pos["shares"],
            "shares": pos["shares"], "lots": pos["shares"] / 100, "entry_value": pos["entry_value"],
            "exit_value": exit_value, "pnl_amount": exit_value - pos["entry_value"],
            "pnl_pct": exit_value / pos["entry_value"] - 1, "duration": holding_sessions,
            "exit_reason": row["reason"], "entry_score": pos.get("score", 0.),
            "entry_nav": pos["entry_nav"], "entry_budget": pos["entry_budget"],
        }
        trades.append(trade)
        positions.pop(symbol)
    execute_sales(571, min(last_bar, 872))
    if error:
        return finish()
    if allow_buy and last_bar >= 872:
        for rank, row in enumerate(ranked_candidates(selected, params).to_dicts(), 1):
            symbol = row["symbol"]
            decision = {"date": str(day), "symbol": symbol, "rank": rank}
            decisions.append(decision)
            if symbol in positions or len(positions) >= params["position_limit"]:
                decision["reason"] = "持仓上限或重复持仓"
                continue
            nav = cash
            for held, pos in positions.items():
                known = frame.filter((pl.col("symbol") == held) & (pl.col("bar") <= 872))
                mark = known["close"][-1] if not known.is_empty() else pos["last_mark"]
                nav += pos["remaining"] * mark
            budget = min(cash, nav * params["entry_nav_fraction"])
            if params["entry_budget"]:
                budget = min(budget, params["entry_budget"])
            buy = fill_order(frame, "buy", symbol, lookup.get(symbol), 872, 872, params,
                             budget=budget, price_field="high")
            decision.update(entry_nav=nav, entry_budget=budget, filled_shares=buy["shares"])
            if not buy["shares"]:
                decision["reason"] = "14:32无可成交容量或含费资金不足"
                continue
            decision["reason"] = "已成交"
            sections["buy"].append({**row, **decision, "buy_price": buy["notional"] / buy["shares"],
                                     "buy_fee": buy["fee"], "buy_value": buy["value"],
                                     "buy_time": "14:32", "participation": params["participation"]})
            cash -= buy["value"]
            fills.extend(buy["fills"])
            positions[symbol] = {
                "symbol": symbol, "name": row.get("name"), "entry_date": str(day), "sell_date": str(next_market_day(day)),
                "shares": buy["shares"], "remaining": buy["shares"], "entry_notional": buy["notional"],
                "entry_value": buy["value"], "entry_nav": nav, "entry_budget": budget,
                "last_buy_time": "14:32", "last_mark": buy["fills"][-1]["price"],
                "score": row.get("return_1430", 0.), "sell_notional": 0., "sell_fee_paid": 0.,
            }
    if last_bar > 872:
        execute_sales(873, last_bar)
    return finish()
