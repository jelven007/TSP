"""通用分钟入场、次日早盘清仓及确认停牌后复牌卖出的研究现金账本。"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from datetime import time as clock

import polars as pl

from app.backtest.costs import buy_quantity_rules
from app.backtest.engine import BacktestEngine, MatcherConfig, TradeRecord
from app.backtest.governance import research_preflight, status_quality
from app.backtest.minute_replay import (
    MinuteSignalReplayer,
    _scalar_limit_up_price,
    minute_panel_start,
    minute_replay_feature_plan,
    requires_full_pool,
)
from app.instrument_suspension import STATUS_COLUMN, dated_status, holding_exit_status
from app.market_time import published_trading_day
from app.price_limits import is_risk_warning_name, price_limit_pct
from app.strategy.morning_liquidation import evaluate, parameters
from app.strategy.overnight_runtime import next_market_day
from app.strategy.risk_policy import strategy_policy, valid_price

EXECUTION = "generic_morning_liquidation_resume_v2"


def run_morning(service, config, strategy, params, overrides, definitions, *,
                progress_cb, cancel_event, result_policy, run_id, t0, provenance):
    from app.backtest.strategy import StrategyBacktestResult, _with_status_fields

    output = StrategyBacktestResult(
        run_id=run_id, config=service._config_to_dict(config), provenance=provenance,
    )
    positions, marks, records, equities, exposures = {}, {}, [], [], []
    cash = float(config.initial_capital)
    counters = defaultdict(int)
    peak = cash
    replayed = 0
    score_min, score_max = service._normalize_score_range(
        overrides.get("score_min"), overrides.get("score_max"),
    )

    def finish(error=None):
        output.error = error
        output.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        output.stats.update({
            "execution_backend": "minute_filter", "minute_execution": EXECUTION,
            "result_scope": "raw_price_cash_ledger_excluding_unverified_dividends",
            "certified": False,
            "status": "rejected_before_NAV" if error else "completed",
            "execution": {
                **dict(counters), "cash": cash,
                "remaining_positions": list(positions.values()),
                "buy_fills": sum(f["side"] == "buy" for f in output.fills),
                "sell_fills": sum(f["side"] == "sell" for f in output.fills),
                "capacity_checked": True, "volume_participation_upper_bound": 1.0,
                "aggregated_order_fees": True,
            },
            "minute_replay": {"replayed_days": replayed},
        })
        if error:
            output.stats["execution"]["partial_equity_curve"] = equities
            output.equity_curve = []
            output.drawdown_curve = []
        output.stats = result_policy.select_stats(output.stats)
        if not result_policy.include_trades:
            output.trades = []
        return output

    if config.mode != "position" or config.asset_type != "stock":
        return finish("早盘清仓收益回测需要A股连续资金组合模式, 暂不支持独立全量模拟")
    exits = service._effective_signals(overrides, "exit_signals", strategy.exit_signals)
    if len(definitions) != 1 or exits != ["csgi_" + definitions[0]["id"]]:
        return finish("早盘清仓收益回测暂不支持混合其他出场触发器")
    sig = definitions[0]
    if sig.get("enabled") is False:
        return finish("早盘清仓信号已禁用, 无法执行清仓收益回测")
    sig_params = parameters(sig)
    deadline_clock = clock.fromisoformat(sig_params["time"])
    fallback_clock = clock.fromisoformat(sig_params.get("fallback_time", sig_params["time"]))
    if any(at <= clock(9, 30) or at.second for at in (deadline_clock, fallback_clock)):
        return finish("早盘清仓收益回测需要09:31至11:30的整分钟止损及兜底时间")
    risk = strategy_policy(strategy, overrides)
    if config.regime_filter or any(v is not None for v in asdict(risk).values()):
        return finish("早盘清仓收益回测暂不支持环境过滤、固定/移动风控或最长持有")
    from app.strategy.entry_risk import EntryRiskPolicy
    if EntryRiskPolicy.from_mapping(overrides.get("entry_risk")).enabled:
        return finish("冻结日线入场风险暂不支持分钟策略")
    if config.exit_fill == "signal_next_minute":
        return finish("早盘清仓收益回测按触发分钟收盘撮合, 暂不支持下一分钟卖出")
    if (not valid_price(cash) or config.max_positions < 1
            or not math.isfinite(config.max_exposure_pct)
            or not 0 <= config.max_exposure_pct <= 1
            or config.position_sizing not in ("equal", "score_weight")):
        return finish("初始资金、持仓数、总暴露或仓位分配设置无效")
    if any(not math.isfinite(v) or v < 0 for v in (
        config.slippage_bps, config.fees_pct,
        config.commission_pct or 0, config.stamp_tax_pct or 0,
    )):
        return finish("回测费用和滑点必须为非负有限数")
    matcher = MatcherConfig(
        fees_pct=config.fees_pct, commission_pct=config.commission_pct,
        stamp_tax_pct=config.stamp_tax_pct, minimum_commission=config.minimum_commission,
        transfer_fee_pct=config.transfer_fee_pct, stamp_tax_mode="historical",
        slippage_bps=0, asset_type="stock",
    )
    slip = config.slippage_bps / 10000
    bf = service._effective_basic_filter(strategy, overrides)
    daily_bars = service.strategy_engine.minute_daily_history_bars(
        [config.strategy_id], overrides_map={config.strategy_id: overrides},
    )
    feature_plan = _with_status_fields(minute_replay_feature_plan(daily_bars, bf, overrides), "stock")
    repo = service.engine.repo
    suspension_history = getattr(repo, "get_suspension_history", lambda: pl.DataFrame())()
    try:
        panel = service.engine.load_panel_for_backtest(
            None if requires_full_pool(service.strategy_engine, strategy, overrides) else config.symbols,
            minute_panel_start(config.start, daily_bars), config.end, feature_plan, asset_type="stock",
        )
    except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
        return finish(f"回测特征准备失败: {exc}")
    if panel.is_empty() or "raw_close" not in panel.columns:
        return finish("早盘清仓收益回测缺少日线数据或原始收盘价")
    from app.strategy import overlay
    from app.strategy.scoring import scoring_warmup_bars
    conditions = overlay.effective_conditions(overrides)
    required_history = max(
        ((20 if name == "amount_mean_20d" else scoring_warmup_bars({name: 1}))
         + max(c["leftDays"], c["rightDays"])
         for c in conditions for name in overlay.fields([c])),
        default=1,
    )
    if panel.filter(pl.col("date") < config.start)["date"].n_unique() < max(
        strategy.minute_daily_bars, required_history,
    ):
        return finish("分钟策略日线预热不足, 无法认证入场条件")
    issues = research_preflight(config, provenance, status_quality(panel, config.start, config.end))
    if config.strict_research and issues:
        return finish("严格研究校验未通过: " + "; ".join(issues))
    output.stats["warnings"] = [
        *issues, "原始现金账本未计未经核验的分红送转; 分钟成交量100%仅为容量上界",
    ]
    output.config.update(
        minute_execution=EXECUTION, minute_exit_fill="trigger_minute_close",
        volume_participation_upper_bound=1.0,
    )
    output.strategy_info = {
        "id": config.strategy_id, "name": strategy.meta.get("name", config.strategy_id),
        "execution_backend": "minute_filter", "minute_execution": EXECUTION,
        "entry_signals": service._effective_signals(overrides, "entry_signals", strategy.entry_signals),
        "exit_signals": exits, "exit_params": sig_params,
        "minute_entry_fill": strategy.meta.get("minute_entry_fill", "signal_close"),
        "minute_exit_fill": "trigger_minute_close", "volume_participation_upper_bound": 1.0,
    } if result_policy.include_strategy_info else {}
    days, day = [], config.start
    while day <= config.end:
        known = published_trading_day(day)
        if known is None:
            return finish("早盘清仓收益回测需要有证据的交易日历")
        if known:
            days.append(day)
        day += timedelta(days=1)
    if not days:
        return finish("区间内无已确认交易日")
    minute_days = set(repo.list_minute_dates(config.start, config.end, "stock"))
    replayer = MinuteSignalReplayer(service.engine, service.strategy_engine)
    next_open = strategy.meta.get("minute_entry_fill") == "next_minute_open"

    def market_value():
        return sum(p["remaining"] * marks[symbol] for symbol, p in positions.items())

    def fee(notional, side, day):
        return (matcher.buy_value(notional) - notional if side == "buy"
                else notional - matcher.sell_value(notional, day))

    def reject_overdue(day):
        overdue = [p for p in positions.values() if p["entry_date"] < day
                   and not p.get("suspension_deferred")]
        if overdue:
            counters["deadline_violations"] += len(overdue)
            detail = "; ".join(
                f"{p['symbol']} 剩余{p['remaining']}股, 已卖{p['shares'] - p['remaining']}/{p['shares']}股"
                for p in overdue
            )
            return finish(f"{day} {sig_params.get('fallback_time', sig_params['time'])}清仓截止后仍有余股, {detail}; 保留实际成交和持仓")
        return None

    try:
        for index, day in enumerate(days):
            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")
            if progress_cb:
                progress_cb({"day": index + 1, "total": len(days), "date": str(day)})
            if day not in minute_days:
                return finish(f"{day} 缺分钟K分区, 无法认证收益")
            previous = next(d for d in reversed(sorted(panel["date"].unique())) if d < day) if (
                panel.filter(pl.col("date") < day).height
            ) else None
            # 日历判定精确T-1, 不能把上一个有数据的日期当成上一交易日。
            prior = day - timedelta(days=1)
            while published_trading_day(prior) is False:
                prior -= timedelta(days=1)
            if previous != prior or published_trading_day(prior) is not True:
                return finish(f"{day} 缺精确前交易日日线数据")
            prev_rows = {r["symbol"]: r for r in panel.filter(pl.col("date") == prior).iter_rows(named=True)}
            current_rows = {r["symbol"]: r for r in panel.filter(pl.col("date") == day).iter_rows(named=True)}
            if not current_rows:
                return finish(f"{day} 缺日线估值数据")
            replay = replayer.replay(
                strategy, panel=panel, start=day, end=day, params=params,
                overrides={**overrides, "exit_signals": []},
                symbols=config.symbols, cancel_event=cancel_event,
            )
            replayed += replay.replayed_days
            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")
            entry_error = ("; ".join(replay.errors) or f"{day} 分钟入场回放数据不完整") if (
                replay.errors or replay.skipped_days
            ) else None
            counters["buy_limit_up"] += replay.buy_limit_up
            counters["buy_no_next_minute"] += replay.buy_no_next_minute
            hits_at = defaultdict(list)
            for hit in replay.hits:
                at = datetime.combine(day, clock.fromisoformat(hit.trigger_time))
                hits_at[(at, 0 if next_open else 1)].append(hit)
                output.signals.append({
                    "date": str(day), "symbol": hit.symbol, "side": "buy",
                    "signal_time": f"{day} {hit.signal_time or hit.trigger_time}",
                    "datetime": str(at), "price": hit.raw_entry_price, "score": hit.score,
                    "signal_bar": at.hour * 60 + at.minute, "reason": "entry",
                })
            symbols = set(positions) | {h.symbol for h in replay.hits}
            history = repo.get_minute_by_dates(sorted(symbols), [day], "stock") if symbols else pl.DataFrame()
            if not history.is_empty() and history.schema["datetime"].time_zone:
                history = history.with_columns(
                    pl.col("datetime").dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None),
                )
            groups = history.partition_by("symbol", as_dict=True) if not history.is_empty() else {}
            triggers, last_sale, suspended = {}, {}, set()
            for pos in positions.values():
                pos["day_sell_notional"] = 0.
            for symbol, pos in positions.items():
                due = next_market_day(pos["entry_date"])
                # 离线预先定位当天已公开的复牌触发点, 成交事件仍受该时点约束。
                status = holding_exit_status(
                    suspension_history, symbol, due, day, observed_at=datetime.combine(day, clock(15)),
                )
                if status == "suspended":
                    suspended.add(symbol)
                    pos["suspension_deferred"] = True
                    counters["suspended_position_days"] += 1
                    continue
                if due != day and status != "resume":
                    return finish(f"{day} {symbol} 已错过精确次交易日清仓")
                frame = groups.get((symbol,), pl.DataFrame())
                prev = prev_rows.get(symbol, {}).get("raw_close")
                if not valid_price(prev) and status == "resume":
                    prior_status = dated_status(
                        suspension_history.filter(pl.col("symbol") == symbol), prior,
                        observed_at=datetime.combine(prior, clock(15)),
                    )
                    if not prior_status.is_empty() and prior_status[STATUS_COLUMN][0] is True:
                        prev = marks.get(symbol)
                if not valid_price(prev):
                    return finish(f"{day} {symbol} 缺原始昨收")
                pos["sell_reference"] = prev
                observation = datetime.combine(day, clock(15) if status == "resume" else deadline_clock)
                if "fallback_time" in sig_params and status != "resume":
                    observation -= timedelta(microseconds=1)
                evaluated = evaluate(
                    frame, sig, day=day, now=observation,
                    positions=[{**pos, "entry_date": str(pos["entry_date"]),
                                "qty": pos["remaining"], "available_qty": pos["remaining"]}],
                    prev_close={symbol: prev}, suspension_history=suspension_history,
                )
                if not evaluated.hits and "fallback_time" in sig_params and status != "resume":
                    evaluated = evaluate(
                        frame, sig, day=day, now=datetime.combine(day, deadline_clock),
                        positions=[{**pos, "entry_date": str(pos["entry_date"]),
                                    "qty": pos["remaining"], "available_qty": pos["remaining"]}],
                        prev_close={symbol: prev}, suspension_history=suspension_history,
                    )
                if not evaluated.hits:
                    return finish(f"{day} {symbol} 无法确定早盘清仓触发")
                hit = evaluated.hits[0]
                at = datetime.fromisoformat(hit["trigger_time"])
                if status == "resume":
                    at = at.replace(second=0, microsecond=0) + timedelta(minutes=1)
                    if clock(11, 31) <= at.time() < clock(13, 1):
                        at = datetime.combine(day, clock(13, 1))
                    if at.time() > clock(15):
                        return finish(f"{day} {symbol} 复牌公告后本交易日无已完成成交分钟")
                    prefix = frame.filter(pl.col("datetime") == at) if not frame.is_empty() else frame
                    expected_times = [at]
                else:
                    prefix = frame.filter(
                        pl.col("datetime").is_between(datetime.combine(day, clock(9, 31)), at),
                    ) if not frame.is_empty() else frame
                    expected = int((at - datetime.combine(day, clock(9, 30))).total_seconds() // 60)
                    expected_times = [datetime.combine(day, clock(9, 31)) + timedelta(minutes=i)
                                      for i in range(expected)]
                if prefix.is_empty() or sorted(prefix["datetime"].to_list()) != expected_times or (
                    prefix.height and any(
                        prefix.filter(
                            ~pl.col(c).is_finite() | (pl.col(c) <= 0) | pl.col(c).is_null(),
                        ).height for c in ("high", "low", "close")
                    )
                ) or (prefix.height and prefix.filter(pl.col("high") < pl.col("low")).height):
                    return finish(f"{day} {symbol} 早盘清仓分钟前缀缺失、重复或无效")
                triggers[symbol] = hit
                last_sale[symbol] = at - timedelta(minutes=1)
                output.signals.append({"date": str(day), "side": "sell", **hit, "signal_id": exits[0],
                                       "signal_bar": at.hour * 60 + at.minute})
            # 只用截至当前事件的分钟价估值与分配预算, 不借未来卖款或日终价格。
            events = defaultdict(list)
            for row in history.iter_rows(named=True):
                if row["datetime"].date() == day:
                    events[row["datetime"]].append(row)
            # 一个分钟标签内, 开盘入场早于收盘清仓, 不可预支稍后的卖款。
            for at, phase in sorted({(at, 1) for at in events} | set(hits_at)):
                if cancel_event is not None and cancel_event.is_set():
                    return finish("cancelled")
                sold_this_event = set()
                rows_at = defaultdict(list)
                for row in events[at]:
                    rows_at[row["symbol"]].append(row)
                    trigger = triggers.get(row["symbol"])
                    before_resume = (trigger and trigger["reason"] == "resume"
                                     and at < datetime.fromisoformat(trigger["trigger_time"]))
                    if row["symbol"] in suspended or before_resume:
                        return finish(f"{day} {row['symbol']} 停牌证据与分钟行情冲突")
                    price_field = "open" if phase == 0 else "close"
                    if valid_price(row.get(price_field)):
                        marks[row["symbol"]] = row[price_field]
                for symbol in list(positions):
                    trigger = triggers.get(symbol)
                    deadline = clock(15) if trigger and trigger["reason"] == "resume" else fallback_clock
                    if (phase == 0 or not trigger
                            or not datetime.fromisoformat(trigger["trigger_time"]) <= at <= datetime.combine(day, deadline)
                            or not (clock(9, 31) <= at.time() <= clock(11, 30)
                                    or clock(13, 1) <= at.time() <= clock(15))):
                        continue
                    pos = positions[symbol]
                    if ("fallback_time" in sig_params and trigger["reason"] != "resume"
                            and (at == datetime.combine(day, fallback_clock)
                                 or (at == datetime.combine(day, deadline_clock)
                                     and trigger["reason"] != "stop_time"))):
                        stage_hit = evaluate(
                            pl.DataFrame(), sig, day=day, now=at,
                            positions=[{**pos, "entry_date": str(pos["entry_date"]),
                                        "qty": pos["remaining"], "available_qty": pos["remaining"]}],
                            prev_close={symbol: pos["sell_reference"]}, suspension_history=suspension_history,
                        ).hits[0]
                        output.signals.append({"date": str(day), "side": "sell", **stage_hit,
                                               "signal_id": exits[0], "signal_bar": at.hour * 60 + at.minute})
                    rows = rows_at.get(symbol, [])
                    expected_at = last_sale[symbol] + timedelta(minutes=1)
                    if expected_at.time() == clock(11, 31):
                        expected_at = datetime.combine(day, clock(13, 1))
                    if len(rows) != 1 or at != expected_at:
                        return finish(f"{day} {symbol} 清仓成交窗口缺失或重复分钟")
                    last_sale[symbol] = at
                    row = rows[0]
                    volume = row.get("volume")
                    if (not valid_price(row.get("close")) or volume is None
                            or not math.isfinite(volume) or volume < 0):
                        return finish(f"{day} {symbol} 清仓成交数据缺失或无效")
                    if volume == 0:
                        counters["sell_no_volume_or_price"] += 1
                        continue
                    name = current_rows.get(symbol, {}).get("name", pos["name"])
                    pct = price_limit_pct(symbol, day, is_risk_warning=bool(
                        current_rows.get(symbol, {}).get("is_risk_warning", is_risk_warning_name(name)),
                    ))
                    down = _scalar_limit_up_price(pos["sell_reference"], -pct)
                    price = row["close"] * (1 - slip)
                    if price <= down + 1e-9:
                        counters["sell_limit_down"] += 1
                        continue
                    quantity = min(pos["remaining"], math.floor(row["volume"] * 100))
                    if quantity <= 0:
                        continue
                    before = pos["day_sell_notional"]
                    notional = quantity * price
                    cost = fee(before + notional, "sell", day) - fee(before, "sell", day)
                    pos["sell_notional"] += notional
                    pos["day_sell_notional"] += notional
                    pos["sell_fees"] += cost
                    pos["remaining"] -= quantity
                    cash += notional - cost
                    sold_this_event.add(symbol)
                    output.fills.append({
                        "date": str(day), "datetime": str(at), "symbol": symbol, "side": "sell",
                        "shares": quantity, "price": price, "notional": notional, "fee": cost,
                        "participation": quantity / (row["volume"] * 100),
                        "bar": at.hour * 60 + at.minute, "minute_volume_shares": row["volume"] * 100,
                    })
                    if not pos["remaining"]:
                        proceeds = pos["sell_notional"] - pos["sell_fees"]
                        record = TradeRecord(
                            symbol=symbol, name=pos["name"], entry_date=pos["entry_date"], exit_date=day,
                            entry_price=pos["price"], exit_price=pos["sell_notional"] / pos["shares"],
                            pnl_pct=proceeds / pos["entry_value"] - 1,
                            duration=sum(published_trading_day(pos["entry_date"] + timedelta(days=i)) is True
                                         for i in range(1, (day - pos["entry_date"]).days + 1)),
                            exit_reason="signal", shares=pos["shares"], lots=pos["shares"] / 100,
                            position_pct=pos["position_pct"], entry_value=pos["entry_value"],
                            exit_value=proceeds, pnl_amount=proceeds - pos["entry_value"],
                            entry_score=pos["score"], entry_signal_date=pos["signal_time"],
                            exit_signal_date=trigger["trigger_time"], exit_signal_id=exits[0],
                            raw_entry_shares=pos["shares"], raw_entry_price=pos["price"],
                        )
                        records.append(record)
                        trade = service._trade_to_dict(record)
                        trade.update(entry_date=pos["fill_time"], exit_date=at.strftime("%Y-%m-%d %H:%M"),
                                     entry_fee=pos["entry_fee"], exit_fee=pos["sell_fees"])
                        output.trades.append(trade)
                        if pos.get("suspension_deferred"):
                            counters["resume_exit_trades"] += 1
                        del positions[symbol]
                        del triggers[symbol]
                        del last_sale[symbol]
                if phase == 1 and at.time() >= fallback_clock:
                    rejected = reject_overdue(day)
                    if rejected is not None:
                        return rejected
                candidates = []
                if entry_error:
                    continue
                for hit in sorted(hits_at[(at, phase)], key=lambda h: (-h.score, h.symbol)):
                    if hit.symbol in positions:
                        continue
                    if hit.symbol in sold_this_event:
                        counters["buy_exit_conflict"] += 1
                        continue
                    if ((score_min is not None and hit.score < score_min)
                            or (score_max is not None and hit.score > score_max)):
                        counters["buy_score_filter"] += 1
                        continue
                    candidates.append(hit)
                slots = config.max_positions - len(positions)
                counters["buy_no_slot"] += max(len(candidates) - slots, 0)
                candidates = candidates[:slots]
                if not candidates:
                    continue
                value = market_value()
                equity = cash + value
                target = equity * config.max_exposure_pct / config.max_positions
                budget = min(cash, equity * config.max_exposure_pct - value, target * len(candidates))
                weights = [max(h.score, 0) for h in candidates] if config.position_sizing == "score_weight" else []
                weights = [v / sum(weights) for v in weights] if sum(weights) > 0 else [1 / len(candidates)] * len(candidates)
                for hit, weight in zip(candidates, weights, strict=True):
                    symbol = hit.symbol
                    rows = rows_at.get(symbol, [])
                    if len(rows) != 1:
                        return finish(f"{day} {symbol} 买入成交分钟缺失或重复")
                    row = rows[0]
                    previous_close = prev_rows.get(symbol, {}).get("raw_close")
                    if not valid_price(previous_close) or not valid_price(hit.raw_entry_price):
                        return finish(f"{day} {symbol} 缺买入原始价或精确昨收")
                    volume = row.get("volume")
                    if volume is None or not math.isfinite(volume) or volume < 0:
                        return finish(f"{day} {symbol} 买入成交数据缺失或无效")
                    if volume == 0:
                        counters["buy_no_volume"] += 1
                        continue
                    price = hit.raw_entry_price * (1 + slip)
                    name = current_rows.get(symbol, {}).get("name", "")
                    up = _scalar_limit_up_price(previous_close, price_limit_pct(
                        symbol, day, is_risk_warning=bool(current_rows.get(symbol, {}).get(
                            "is_risk_warning", is_risk_warning_name(name),
                        )),
                    ))
                    if price >= up - 1e-9:
                        counters["buy_limit_up"] += 1
                        continue
                    value = market_value()
                    capacity = (cash + value) * config.max_exposure_pct - value
                    allocation = min(budget * weight, target, cash, capacity)
                    minimum, step, _ = buy_quantity_rules(symbol)
                    quantity = min(matcher.buy_quantity(max(allocation, 0), price, symbol),
                                   math.floor(row["volume"] * 100 / step) * step)
                    if quantity < minimum:
                        counters["buy_lot_size"] += 1
                        continue
                    notional = quantity * price
                    cost = fee(notional, "buy", day)
                    entry_value = notional + cost
                    if entry_value > cash + 1e-8 or entry_value > capacity + 1e-8:
                        return finish("买入资金守恒校验失败")
                    cash -= entry_value
                    positions[symbol] = {
                        "symbol": symbol, "name": name, "entry_date": day, "remaining": quantity,
                        "shares": quantity, "price": price, "entry_value": entry_value,
                        "entry_fee": cost, "position_pct": entry_value / equity, "score": hit.score,
                        "signal_time": f"{day} {hit.signal_time or hit.trigger_time}",
                        "fill_time": at.strftime("%Y-%m-%d %H:%M"), "sell_notional": 0., "sell_fees": 0.,
                    }
                    marks[symbol] = row["open" if phase == 0 else "close"]
                    output.fills.append({
                        "date": str(day), "datetime": str(at), "symbol": symbol, "side": "buy",
                        "shares": quantity, "price": price, "notional": notional, "fee": cost,
                        "participation": quantity / (row["volume"] * 100),
                        "bar": at.hour * 60 + at.minute, "minute_volume_shares": row["volume"] * 100,
                    })
            for symbol, trigger in triggers.items():
                if symbol in positions and trigger["reason"] == "resume":
                    if last_sale[symbol] != datetime.combine(day, clock(15)):
                        return finish(f"{day} {symbol} 复牌卖出成交窗口未完整覆盖至收盘")
                    counters["resume_pending_position_days"] += 1
            rejected = reject_overdue(day)
            if rejected is not None:
                return rejected
            if entry_error:
                return finish(entry_error)
            for symbol in positions:
                if symbol in suspended:
                    continue
                mark = current_rows.get(symbol, {}).get("raw_close")
                if not valid_price(mark):
                    return finish(f"{day} {symbol} 缺日终原始收盘估值")
                marks[symbol] = mark
                positions[symbol]["last_mark_date"] = str(day)
            value = market_value()
            nav = cash + value
            peak = max(peak, nav)
            exposure = value / nav if nav > 0 else 0.
            equities.append({"date": str(day), "value": nav, "cash": cash,
                             "positions": len(positions), "exposure": exposure,
                             "suspended_marks": [
                                 {"symbol": symbol, "price": marks[symbol],
                                  "shares": positions[symbol]["remaining"],
                                  "last_price_date": positions[symbol]["last_mark_date"]}
                                 for symbol in sorted(suspended) if symbol in positions
                             ]})
            exposures.append(exposure)
            output.drawdown_curve.append({"date": str(day), "value": nav / peak - 1})
    except (ValueError, OSError, KeyError, pl.exceptions.PolarsError) as exc:
        return finish(f"分钟收益撮合失败: {exc}")
    output.stats.update(BacktestEngine._calc_portfolio_stats_from_values(
        [e["value"] for e in equities], exposures, records, config.initial_capital,
        include_monte_carlo=result_policy.include_monte_carlo,
    ))
    output.stats.update(
        total_return=equities[-1]["value"] / config.initial_capital - 1,
        max_drawdown=min(d["value"] for d in output.drawdown_curve),
        unclosed_positions=len(positions),
        unrealized_pnl_amount=market_value() - sum(
            p["entry_value"] * p["remaining"] / p["shares"] for p in positions.values()),
        unclosed_realized_pnl_amount=sum(
            p["sell_notional"] - p["sell_fees"] - p["entry_value"] * (1 - p["remaining"] / p["shares"])
            for p in positions.values()),
    )
    output.equity_curve = equities if result_policy.include_curves else []
    if not result_policy.include_curves:
        output.drawdown_curve = []
    if result_policy.include_per_symbol_stats:
        output.per_symbol_stats = BacktestEngine._calc_per_symbol(records)
    if result_policy.include_benchmark:
        output.benchmark_curve = service._build_benchmark_curve(config.start, config.end)
    return finish()
