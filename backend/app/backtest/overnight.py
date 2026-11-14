"""可选隔夜分钟组合撮合。按日读分钟分区, 现金及余股始终按实际成交记账。"""
from __future__ import annotations

import math
import time
from dataclasses import replace
from datetime import date, datetime, timedelta

import polars as pl

from app.backtest.costs import buy_quantity_rules
from app.backtest.engine import BacktestEngine, TradeRecord
from app.backtest.entry_execution import execute_entry_window, expected_trading_minutes
from app.backtest.governance import research_preflight, status_quality
from app.backtest.minute_replay import minute_panel_start, minute_replay_feature_plan
from app.strategy.builtin._overnight import (
    entry_budget,
    exit_signal,
    minute_frame,
    minute_number,
    morning_features,
    parameters,
    suspended_on,
)
from app.strategy.engine import StrategyDataContext
from app.strategy.overnight_runtime import annotate_daily, next_market_day


def order_fee(notional: float, side: str, params: dict) -> float:
    if notional <= 0:
        return 0.
    return (
        params.get("transfer_fee_fixed", 0.)
        + max(params["minimum_commission"], notional * params["commission"])
        + notional * (params["transfer_fee"] + (params["stamp_tax"] if side == "sell" else 0.))
    )


def fill_order(frame, side, symbol, prior_close, first_bar, last_bar, values, *,
               budget=None, quantity=None, price_field=None, prior_notional=0.):
    """一只股票、一天、一个方向的汇总委托; 分片费用为汇总费用的增量。"""
    params = parameters(values)
    # 整笔买入预算预留固定费; 各分片仍只记累计费用的增量。
    buying_budget = max(0., (budget or 0.) - params.get("transfer_fee_fixed", 0.))
    minimum, step, maximum = buy_quantity_rules(symbol)
    notional, shares, fills = 0., 0, []
    target = quantity
    if side not in ("buy", "sell"):
        raise ValueError("未知委托方向")
    if prior_close is None or not math.isfinite(prior_close) or prior_close <= 0:
        return {"fills": [], "shares": 0, "remaining": quantity or 0,
                "notional": 0., "fee": 0., "value": 0.}
    pct = .2 if symbol.startswith(("300", "301", "688", "689")) else .1
    down, up = [math.floor(prior_close * (1 + sign * pct) * 100 + .5) / 100
                for sign in (-1, 1)]
    source = minute_frame(frame)
    if not source.is_empty():
        source = source.filter((pl.col("symbol") == symbol)
                               & pl.col("bar").is_between(first_bar, last_bar)
                               & (pl.col("bar").is_between(571, 690)
                                  | pl.col("bar").is_between(781, 900)))
        if source["bar"].n_unique() != source.height:
            raise ValueError("成交窗口含重复分钟")
        for row in source.iter_rows(named=True):
            if not all(row.get(f) is not None and math.isfinite(float(row[f])) and float(row[f]) > 0
                       for f in ("high", "low", "close", "volume")):
                continue
            if ((side == "buy" and row["low"] >= up - 1e-9)
                or (side == "sell" and row["high"] <= down + 1e-9)):
                continue
            base = row.get(price_field) if price_field else (row["high"] + row["low"] + row["close"]) / 3
            if base is None or not math.isfinite(base) or base <= 0:
                continue
            price = min(up, max(down, base * (
                1 if price_field == "execution_price" or (side == "buy" and price_field == "high")
                else 1 + (params["slippage"] if side == "buy" else -params["slippage"]))))
            capacity = math.floor(row["volume"] * 100 * params["participation"] / step + 1e-9) * step
            if target is None:
                cap_notional = min(
                    buying_budget / (1 + params["commission"] + params["transfer_fee"]),
                    (buying_budget - params["minimum_commission"]) / (1 + params["transfer_fee"]),
                )
                target = min(maximum, math.floor(max(cap_notional, 0) / price / step + 1e-9) * step)
            qty = min(max(target - shares, 0), capacity)
            if side == "buy":
                # 价格改变时重新限制含费成本; 不增加最初目标数量。
                cap = min(
                    buying_budget / (1 + params["commission"] + params["transfer_fee"]),
                    (buying_budget - params["minimum_commission"]) / (1 + params["transfer_fee"]),
                ) - notional
                qty = min(qty, math.floor(max(cap, 0) / price / step + 1e-9) * step)
            if qty < minimum and not (side == "sell" and qty == target - shares and qty > 0):
                continue
            before_fee = order_fee(prior_notional + notional, side, params)
            notional += qty * price
            shares += qty
            fills.append({
                "date": str(row["datetime"].date()), "symbol": symbol, "side": side,
                "bar": row["bar"], "datetime": row["datetime"].isoformat(sep=" "),
                "shares": qty, "price": price, "notional": qty * price,
                "fee": order_fee(prior_notional + notional, side, params) - before_fee,
                "minute_volume_shares": row["volume"] * 100,
                "participation": qty / (row["volume"] * 100),
            })
            if shares == target:
                break
    fee = order_fee(prior_notional + notional, side, params) - order_fee(prior_notional, side, params)
    return {"fills": fills, "shares": shares, "remaining": max((target or 0) - shares, 0),
            "notional": notional, "fee": fee,
            "value": notional + fee if side == "buy" else notional - fee}


def run_overnight(service, config, strategy, params, overrides, *,
                  progress_cb, cancel_event, result_policy, run_id, t0, provenance):
    from app.backtest.strategy import StrategyBacktestResult, _with_status_fields

    high_entry = strategy.meta.get("entry_price_field") == "high"
    execution = strategy.meta["minute_execution"]
    from app.strategy.s01_signals import (
        bound_definitions,
        optional_exit_hits,
        optional_triggers,
        resolve_parameters,
        trigger_definitions,
    )

    selectable = optional_triggers(strategy)
    selected = trigger_definitions(service.strategy_engine, strategy, overrides) if selectable else None
    references = {key: service.strategy_engine._effective_signals(
        overrides, key, getattr(strategy, key)) for key in ("entry_signals", "exit_signals")}
    parameter_signals = bound_definitions(service.strategy_engine, strategy, overrides=overrides)
    params = resolve_parameters(service.strategy_engine, strategy, params, definitions=parameter_signals)
    equal_selected = strategy.meta.get("entry_allocation") == "equal_selected_v1"
    if equal_selected:
        params = {**params, "entry_nav_fraction": 1.}
    params = parameters(params)
    reported_params = {k: v for k, v in params.items()
                       if not equal_selected or k != "entry_nav_fraction"}
    allocation = {"entry_allocation": "equal_selected_v1"} if equal_selected else {}
    signal_bar = 885
    first_buy, last_buy = (887, 889) if high_entry else (887, 891)
    entry_signal = parameter_signals.get("entry")
    entry_confirmation = None
    if entry_signal and "time" in entry_signal.get("params", {}):
        from app.strategy.s01_signals import entry_clock

        _, cutoff, entry_confirmation = entry_clock(entry_signal, config.start)
        signal_bar = cutoff.hour * 60 + cutoff.minute
        first_buy = entry_confirmation.hour * 60 + entry_confirmation.minute + 1
        window = expected_trading_minutes(datetime.combine(
            config.start, datetime.min.time()) + timedelta(minutes=first_buy))
        last_buy = window[-1].hour * 60 + window[-1].minute if window else 900
    effective_config = {
        **service._config_to_dict(config), "params": reported_params, **allocation,
        "initial_capital": params["initial_cash"], "max_positions": params["position_limit"],
        "commission_pct": params["commission"], "minimum_commission": params["minimum_commission"],
        "transfer_fee_pct": params["transfer_fee"], "stamp_tax_pct": params["stamp_tax"],
        "transfer_fee_fixed": params.get("transfer_fee_fixed", 0.),
        "slippage_bps": params["slippage"] * 10000, "minute_execution": execution,
        "entry_fill": "close_t", "exit_fill": "close_t",
        "execution_entry_fill": "single_minute_high_capacity" if high_entry else "system_five_minute_high_capacity_v1",
        "execution_exit_fill": "persistent_signal_capacity_window",
        **({"parameter_signals": parameter_signals, **references,
            "trigger_definitions": selected} if selectable else
           {"parameter_signals": parameter_signals,
            "entry_signals": ["csgi_" + parameter_signals["entry"]["id"]],
            "exit_signals": ["csgi_" + parameter_signals["exit"]["id"]]} if parameter_signals else {}),
    }
    output = StrategyBacktestResult(run_id=run_id, config=effective_config, provenance=provenance)
    output.strategy_info = {
        "id": strategy.meta["id"], "name": overrides.get("name") or strategy.meta["name"],
        "description": strategy.meta["description"], "execution_backend": "minute_filter",
        "minute_execution": strategy.meta["minute_execution"], "params": reported_params, **allocation,
    }
    fills, signals, trades, equities, failures = [], [], [], [], []
    suspensions, resumptions = [], []
    positions = {}
    cash = params["initial_cash"]
    repo = service.engine.repo

    def finish(error=None):
        output.error = error
        output.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        output.fills = fills
        output.signals = signals
        output.trades = trades
        output.stats.update({
            "minute_execution": execution,
            "execution": {"fill_count": len(fills), "buy_fills": sum(f["side"] == "buy" for f in fills),
                          "sell_fills": sum(f["side"] == "sell" for f in fills),
                          "deadline_violations": len(failures), "failures": failures,
                          "remaining_positions": list(positions.values()), "cash": cash,
                          "suspensions": suspensions, "resumptions": resumptions,
                          "capacity_checked": True, "aggregated_order_fees": True},
            "result_scope": "raw_price_cash_ledger_excluding_unverified_dividends",
            "certified": False,
        })
        if error:
            output.stats["status"] = "rejected_before_NAV"
            output.stats["execution"]["partial_equity_curve"] = equities
        return output

    if config.mode != "position":
        return finish("隔夜容量执行需要连续资金组合模式, 不能使用独立全量模拟")
    if config.asset_type != "stock":
        return finish("隔夜分钟执行仅支持沪深A股")
    if equal_selected and params["position_limit"] > 4:
        return finish("按入选数量等分资金的S01持仓上限不能超过4只")
    if config.regime_filter or (not selectable and not parameter_signals and any(
        overrides.get(k) for k in ("entry_signals", "exit_signals")
    )) or overrides.get("entry_risk") or (not selectable and any(overrides.get(k) for k in (
        "overlay_filter", "scoring",
        "stop_loss", "take_profit", "trailing_stop", "trailing_take_profit_activate",
        "trailing_take_profit_drawdown",
    ))):
        return finish("隔夜分钟执行请在策略参数中设置选股、卖出、资金和费用规则")
    try:
        from app.strategy.scoring import effective_strategy_scoring

        history_days = 2
        basic_filter = {**(strategy.basic_filter or {}), **(overrides.get("basic_filter") or {})}
        plan = _with_status_fields(minute_replay_feature_plan(
            history_days, basic_filter, {**references, **overrides,
                "scoring": effective_strategy_scoring(strategy.meta, overrides)} if selectable else None), "stock")
        history_days = max(history_days, plan.warmup_bars)
        panel = service.engine.load_panel_for_backtest(
            None, minute_panel_start(config.start, history_days), config.end, plan, asset_type="stock")
        if panel.is_empty():
            return finish("回测区间缺少原始日线数据")
        panel = annotate_daily(panel, repo)
        dates = panel["date"].unique().sort().to_list()
        days = [d for d in dates if config.start <= d <= config.end]
        # 交易日历与日线轴相互核对; 缺整日日线不能把下一有数据日当成T+1。
        from app.market_time import published_trading_day

        for offset in range((config.end - config.start).days + 1):
            expected = config.start + timedelta(days=offset)
            known = published_trading_day(expected)
            if known is None:
                return finish(f"{expected}缺少有证据的交易日历")
            if known and expected not in days:
                return finish(f"{expected}缺少交易日日线, 拒绝跳过交易日")
        if not days:
            return finish("正式区间无交易日")
        universe = panel["symbol"].unique().to_list()
        issues = research_preflight(
            replace(config, stamp_tax_pct=params["stamp_tax"]), provenance,
            status_quality(panel, config.start, config.end))
        output.stats["warnings"] = [*issues, "当前证券目录及已记录ST排除; 完整历史证券池与公司行为未经认证",
                                   "此结果重建买入撮合, 冻结190笔买入账本的历史收益需单独核验"]
        if config.strict_research and issues:
            return finish("严格研究校验未通过: " + "; ".join(issues))
        minute_dates = set(repo.list_minute_dates(config.start, config.end, "stock"))
        for index, day in enumerate(days):
            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")
            if progress_cb:
                progress_cb({"day": index + 1, "total": len(days), "date": str(day)})
            if day not in minute_dates:
                return finish(f"{day}缺少分钟分区, 拒绝跳过交易日")
            frame = repo.get_minute_by_dates(universe, [day], "stock")
            if frame.is_empty() and not (
                positions and all(suspended_on(symbol, day, params) for symbol in positions)
            ):
                return finish(f"{day}分钟分区为空")
            frame = minute_frame(frame)
            frame = frame.filter(pl.col("datetime").dt.date() == day)
            prior_dates = [d for d in dates if d < day]
            if len(prior_dates) < 2:
                return finish(f"{day}缺少两个前交易日原始行情")
            prior = panel.filter(pl.col("date") == prior_dates[-1])
            daily = panel.filter(pl.col("date").is_in(prior_dates[-history_days:]))
            lookup = {r["symbol"]: r["raw_close"] for r in prior.to_dicts()}
            late_sale_cash, late_sale_positions = 0., {}
            # 卖出先于当天尾盘买入, 预算只使用已经到账的卖出净额。
            for symbol, pos in list(positions.items()):
                if day < pos["sell_date"]:
                    continue
                history = frame.filter(pl.col("symbol") == symbol)
                if not selectable and morning_features(history, pos["entry_value"] / pos["shares"]) is None:
                    return finish(f"{day} {symbol}缺少完整105根有效早盘, 拒绝认证收益")
                pending = pos.get("pending")
                if selectable:
                    exit_context = StrategyDataContext(
                        "stock", "1m", day, history=history, current=prior, daily_history=daily,
                        completed_through=datetime.combine(day, datetime.min.time()).replace(hour=15),
                        positions=[{**pos, "qty": pos["remaining"], "available_qty": pos["remaining"],
                                    "entry_unit_cost": pos["entry_value"] / pos["shares"],
                                    "entry_date": str(pos["entry_date"]), "sell_date": str(pos["sell_date"])}],
                        holding_history_loader=repo.get_minute_by_dates)
                    hits = optional_exit_hits(service.strategy_engine, strategy, exit_context,
                        params, selected["exit"], overrides=overrides)
                    if exit_context.exit_errors:
                        return finish("; ".join(exit_context.exit_errors))
                    pending = pending or next(iter(hits), None)
                    if pending is None:
                        continue
                    if (any(s.get("template") == "s01_morning_exit"
                            and s["column"] in pending["signals"] for s in selected["exit"])
                            and morning_features(history, pos["entry_value"] / pos["shares"]) is None):
                        return finish(f"{day} {symbol}缺少完整105根有效早盘, 拒绝认证收益")
                else:
                    for bar in range(675, minute_number(params["guard_time"]) + 1):
                        pending = exit_signal(history, bar, pos["entry_value"] / pos["shares"], params, pending)
                        if pending:
                            break
                pos["pending"] = pending
                signals.append({"date": str(day), "symbol": symbol, **pending})
                sale = fill_order(history, "sell", symbol, lookup.get(symbol),
                                  pending["first_eligible_bar"], pending["last_eligible_bar"],
                                  params, quantity=pos["remaining"])
                fills.extend(sale["fills"])
                late_fills = [f for f in sale["fills"] if f["bar"] >= first_buy] if selectable else []
                late_value = sum(f["notional"] - f["fee"] for f in late_fills)
                if late_fills:
                    # A later exit cannot fund or free a slot for the 14:47 entry batch.
                    late_sale_positions[symbol] = {
                        **pos, "remaining": sum(f["shares"] for f in late_fills)}
                    late_sale_cash += late_value
                cash += sale["value"] - late_value
                pos["remaining"] -= sale["shares"]
                if pos["remaining"]:
                    failures.append({"date": str(day), "symbol": symbol, "remaining": pos["remaining"]})
                    return finish(f"{day} {symbol}{params['sell_deadline']}到期剩余{pos['remaining']}股, 保留实际持仓")
                cycle_notional = pos.get("sell_notional", sale["notional"])
                cycle_value = pos.get("sell_value", sale["value"])
                record = TradeRecord(
                    symbol=symbol, entry_date=pos["entry_date"], exit_date=day,
                    entry_price=pos["entry_notional"] / pos["shares"],
                    exit_price=cycle_notional / pos["shares"],
                    pnl_pct=cycle_value / pos["entry_value"] - 1,
                    duration=dates.index(day) - dates.index(pos["entry_date"]),
                    exit_reason=pending["reason"], shares=pos["shares"], lots=pos["shares"] / 100,
                    entry_value=pos["entry_value"], exit_value=cycle_value,
                    pnl_amount=cycle_value - pos["entry_value"], entry_score=pos["score"],
                    raw_entry_shares=pos["shares"], raw_entry_price=pos["entry_notional"] / pos["shares"],
                )
                trade = service._trade_to_dict(record)
                trade["entry_date"] = f"{pos['entry_date']} {pos['last_buy_time']}"
                trade["exit_date"] = f"{day} {sale['fills'][-1]['datetime'][11:16]}"
                trade["entry_signal_date"] = f"{pos['entry_date']} {signal_bar // 60:02}:{signal_bar % 60:02}"
                trade["exit_signal_date"] = f"{day} {pending['signal_bar'] // 60:02}:{pending['signal_bar'] % 60:02}"
                trade["exit_signal_id"] = "overnight_" + pending["reason"]
                trade["entry_nav"] = pos["entry_nav"]
                trade["entry_budget"] = pos["entry_budget"]
                trades.append(trade)
                positions.pop(symbol)
            # 区间最后一天保留现金, 不发出无法在区间内完成T+1的买入。
            sell_date = next_market_day(day)
            if sell_date <= config.end:
                if sell_date not in dates:
                    return finish(f"{sell_date}缺少次交易日日线")
                result = service.strategy_engine.run(
                    strategy.meta["id"], StrategyDataContext(
                        "stock", "1m", day, current=prior, history=frame, daily_history=daily,
                        observed_at=entry_confirmation.replace(
                            year=day.year, month=day.month, day=day.day) if entry_confirmation else None),
                    pool=config.symbols, params=params, overrides=overrides)
                buy_start = first_buy
                entry_symbols = {h["symbol"] for h in result.entry_signal_hits}
                buy_rows = result.rows
                entry_positions = {**positions, **late_sale_positions}
                batch_budget = None
                if equal_selected:
                    slots = max(0, params["position_limit"] - len(entry_positions))
                    seen = set(entry_positions)
                    buy_rows = []
                    for row in result.rows:
                        symbol = row["symbol"]
                        if symbol in seen or (selectable and references["entry_signals"]
                                              and symbol not in entry_symbols):
                            continue
                        seen.add(symbol)
                        if len(buy_rows) < slots:
                            buy_rows.append(row)
                    if buy_rows:
                        batch_budget = cash / len(buy_rows)
                        if params["entry_budget"]:
                            batch_budget = min(batch_budget, params["entry_budget"])
                for row in buy_rows:
                    symbol = row["symbol"]
                    if selectable and references["entry_signals"] and symbol not in entry_symbols:
                        continue
                    if symbol in entry_positions or len(entry_positions) >= params["position_limit"]:
                        continue
                    # 后续委托不回到先前成交分钟之前; 只用已观测价格估值。
                    nav = cash
                    for held_symbol, p in entry_positions.items():
                        known = frame.filter(
                            (pl.col("symbol") == held_symbol) & (pl.col("bar") <= buy_start))
                        mark = known["close"][-1] if not known.is_empty() else p["entry_mark"]
                        nav += p["remaining"] * mark
                    budget = (min(cash, batch_budget) if equal_selected
                              else entry_budget(nav, cash, params))
                    if high_entry:
                        buy = fill_order(frame, "buy", symbol, lookup.get(symbol),
                                         buy_start, last_buy, params, budget=budget,
                                         price_field="high")
                    else:
                        minimum, step, maximum = buy_quantity_rules(symbol)

                        def size_order(price, budget=budget, minimum=minimum, step=step, maximum=maximum):
                            cap = min(
                                (budget - params.get("transfer_fee_fixed", 0.)) /
                                (1 + params["commission"] + params["transfer_fee"]),
                                (budget - params.get("transfer_fee_fixed", 0.) -
                                 params["minimum_commission"]) / (1 + params["transfer_fee"]))
                            target = min(maximum, math.floor(max(cap, 0) / price / step) * step)
                            return target if target >= minimum else 0

                        start_at = datetime.combine(day, datetime.min.time()) + timedelta(minutes=buy_start)
                        order = execute_entry_window(
                            frame, symbol=symbol, start_at=start_at,
                            prior_close=lookup.get(symbol), budget=budget,
                            size_order=size_order,
                            buy_value=lambda n: n + order_fee(n, "buy", params),
                            window_minutes=sum(moment.hour * 60 + moment.minute <= last_buy
                                               for moment in expected_trading_minutes(start_at)))
                        buy = {"shares": order.filled_shares, "notional": order.notional,
                               "value": order.value, "fills": [
                                   {"date": str(f.datetime.date()), "symbol": symbol, "side": "buy",
                                    "bar": f.bar, "datetime": f.datetime.isoformat(sep=" "),
                                    "shares": f.shares, "price": f.price, "notional": f.notional,
                                    "fee": f.fee, "minute_volume_shares": f.minute_volume_shares,
                                    "participation": f.participation} for f in order.fills]}
                    if buy["shares"] <= 0:
                        continue
                    cash -= buy["value"]
                    fills.extend(buy["fills"])
                    positions[symbol] = {
                        "symbol": symbol, "entry_date": day, "sell_date": sell_date,
                        "shares": buy["shares"], "remaining": buy["shares"],
                        "entry_notional": buy["notional"], "entry_value": buy["value"],
                        "entry_nav": nav, "entry_budget": budget,
                        "entry_mark": frame.filter(
                            (pl.col("symbol") == symbol)
                            & (pl.col("bar") == buy["fills"][-1]["bar"]))["close"].item(),
                        "last_buy_time": buy["fills"][-1]["datetime"][11:16], "score": row.get("score", 0.),
                        "entry_time": buy["fills"][-1]["datetime"],
                        "last_mark": buy["fills"][-1]["price"],
                    }
                    entry_positions[symbol] = positions[symbol]
                    if high_entry:
                        buy_start = buy["fills"][-1]["bar"]
            cash += late_sale_cash
            marks = {r["symbol"]: r["raw_close"] for r in panel.filter(pl.col("date") == day).to_dicts()}
            market_value = 0.
            for symbol, pos in positions.items():
                price = marks.get(symbol)
                if pos.get("suspended"):
                    price = pos["last_mark"]
                if price is None or not math.isfinite(price) or price <= 0:
                    return finish(f"{day} {symbol}缺少原始收盘估值")
                market_value += pos["remaining"] * price
                pos["last_mark"] = price
            equities.append({"date": str(day), "value": cash + market_value,
                             "cash": cash, "positions": len(positions)})
    except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
        return finish(str(exc))
    values = [e["value"] for e in equities]
    records = [TradeRecord(
        symbol=t["symbol"], entry_date=date.fromisoformat(t["entry_date"][:10]),
        exit_date=date.fromisoformat(t["exit_date"][:10]), entry_price=t["entry_price"],
        exit_price=t["exit_price"], pnl_pct=t["pnl_pct"], duration=t["duration"], exit_reason=t["exit_reason"],
        entry_value=t["entry_value"], exit_value=t["exit_value"], pnl_amount=t["pnl_amount"],
    ) for t in trades]
    stats = BacktestEngine._calc_portfolio_stats_from_values(
        [params["initial_cash"], *values], [1 - e["cash"] / e["value"] for e in equities],
        records, params["initial_cash"], include_monte_carlo=result_policy.include_monte_carlo)
    peak, drawdowns = params["initial_cash"], []
    for equity in equities:
        peak = max(peak, equity["value"])
        drawdowns.append({"date": equity["date"], "value": equity["value"] / peak - 1})
    stats["annual_return"] = (values[-1] / params["initial_cash"]) ** (252 / len(values)) - 1
    stats["max_drawdown"] = min(d["value"] for d in drawdowns)
    stats["total_return"] = values[-1] / params["initial_cash"] - 1
    stats["execution_backend"] = "minute_filter"
    output.stats.update(stats)
    output.equity_curve = equities if result_policy.include_curves else []
    output.drawdown_curve = drawdowns if result_policy.include_curves else []
    output.benchmark_curve = service._build_benchmark_curve(config.start, config.end) if result_policy.include_benchmark else []
    return finish()
