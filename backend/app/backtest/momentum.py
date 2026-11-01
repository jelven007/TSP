"""逐分钟价格退出, 汇总每股票每日方向的最低佣金。"""
import polars as pl

from app.backtest.overnight import fill_order
from app.strategy.builtin._overnight import minute_frame, minute_number
from app.strategy.momentum_rules import exit_quote, parameters


def sell_position(frame, symbol, prior_close, position, values):
    params = parameters(values)
    deadline = minute_number(params["sell_deadline"])
    source = minute_frame(frame)
    if not source.is_empty():
        source = source.filter((pl.col("symbol") == symbol) & pl.col("bar").is_between(571, deadline))
        if source["bar"].n_unique() != source.height:
            raise ValueError("卖出窗口含重复分钟")
    state = dict(position)
    state.setdefault("sell_notional", 0.)
    state.setdefault("sell_fee_paid", 0.)
    sale_day = str(source["datetime"][0].date()) if not source.is_empty() else position.get("sell_fee_date")
    if state.get("sell_fee_date") != sale_day:
        state["day_sell_notional"] = 0.
    state.setdefault("day_sell_notional", 0.)
    initial_notional = state["sell_notional"]
    stopped = state.get("stop_pending", False)
    fills, signals, reasons, fee = [], [], set(), 0.
    for row in source.iter_rows(named=True):
        decision = exit_quote(row, state, params, stopped=stopped)
        if decision is None:
            continue
        reason, price = decision
        stopped |= reason == "net_stop"
        if reason not in reasons:
            signals.append({"signal_bar": row["bar"], "first_eligible_bar": row["bar"],
                            "last_eligible_bar": deadline, "reason": reason})
            reasons.add(reason)
        sale = fill_order(
            pl.DataFrame([dict(row, execution_price=price)]), "sell", symbol, prior_close,
            row["bar"], row["bar"], params, quantity=state["remaining"],
            price_field="execution_price", prior_notional=state["day_sell_notional"])
        fills.extend(sale["fills"])
        state["sell_notional"] += sale["notional"]
        state["day_sell_notional"] += sale["notional"]
        state["sell_fee_paid"] += sale["fee"]
        state["remaining"] -= sale["shares"]
        fee += sale["fee"]
        if not state["remaining"]:
            break
    notional = state["sell_notional"] - initial_notional
    return {"fills": fills, "signals": signals, "remaining": state["remaining"],
            "shares": position["remaining"] - state["remaining"], "notional": notional,
            "fee": fee, "value": notional - fee, "stop_pending": stopped,
            "day_sell_notional": state["day_sell_notional"], "sell_fee_date": sale_day}
