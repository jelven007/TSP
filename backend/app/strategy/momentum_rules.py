"""净止损、毛止盈和分钟容量退出; 同分钟双触发采用止损优先。"""
import math

from app.strategy.builtin._overnight import minute_number
from app.strategy.builtin._overnight import parameters as overnight_parameters

EXECUTION = "momentum_capacity_v1"


def parameters(values):
    params = overnight_parameters({
        "commission": .0002, "transfer_fee": 0., "participation": .5,
        "stop_loss_pct": .06, "take_profit_pct": .06,
        "guard_time": "10:30", "sell_deadline": "11:30",
        **(values or {}), "_execution": EXECUTION,
    })
    for key in ("stop_loss_pct", "take_profit_pct"):
        if not isinstance(params[key], (float, int)) or not math.isfinite(params[key]) or not 0 < params[key] < 1:
            raise ValueError("止损止盈必须使用大于0且小于1的小数比例")
    for lower, upper in (("prior_return_min", "prior_return_max"),
                         ("current_return_min", "current_return_max"),
                         ("float_cap_min", "float_cap_max")):
        if lower not in params and upper not in params:
            continue
        lo, hi = params.get(lower), params.get(upper)
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lo, hi)) or lo > hi:
            raise ValueError("选股阈值必须为有限数值且下限不能超过上限")
    for key in ("amount20_min", "market_positive_min"):
        if key in params and (not isinstance(params[key], (int, float))
                              or not math.isfinite(params[key]) or params[key] < 0
                              or (key == "market_positive_min" and params[key] > 1)):
            raise ValueError("选股成交额或上涨比例阈值无效")
    return params


def sell_fee(notional, params):
    return (max(params["minimum_commission"], notional * params["commission"])
            + notional * (params["transfer_fee"] + params["stamp_tax"])) if notional > 0 else 0.


def net_proceeds(quote, position, params):
    previous = position.get("sell_notional", 0.)
    remaining_notional = quote * (1 - params["slippage"]) * position["remaining"]
    day_notional = position.get("day_sell_notional", 0.)
    paid = position.get("sell_fee_paid", 0.)
    future_fee = sell_fee(day_notional + remaining_notional, params) - sell_fee(day_notional, params)
    return previous + remaining_notional - paid - future_fee


def stop_quote(position, params):
    low, high = 0, math.ceil(position["entry_value"] / max(position["remaining"], 1) * 100)
    while low < high:
        mid = (low + high) // 2
        if net_proceeds(mid / 100, position, params) >= position["entry_value"] * (1 - params["stop_loss_pct"]):
            high = mid
        else:
            low = mid + 1
    return low / 100


def target_quote(position, params):
    return math.ceil(position["entry_notional"] / position["shares"]
                     * (1 + params["take_profit_pct"]) * 100 - 1e-9) / 100


def exit_quote(row, position, params, *, stopped=False):
    if not all(isinstance(row.get(f), (int, float)) and math.isfinite(row[f]) and row[f] > 0
               for f in ("open", "high", "low", "close")):
        return None
    stop = stop_quote(position, params)
    if stopped or net_proceeds(row["low"], position, params) <= position["entry_value"] * (1 - params["stop_loss_pct"]) + 1e-9:
        quote = row["open"] if stopped or row["open"] <= stop else stop
        return "net_stop", quote * (1 - params["slippage"])
    target = target_quote(position, params)
    if row["high"] >= target - 1e-9:
        return "gross_target", max(target, row["open"])
    if position.get("time_guard_pending"):
        return "time_guard", row["open"] * (1 - params["slippage"])
    if minute_number(params["guard_time"]) <= row["bar"] <= minute_number(params["sell_deadline"]):
        return "time_guard", row["close"] * (1 - params["slippage"])
    return None
