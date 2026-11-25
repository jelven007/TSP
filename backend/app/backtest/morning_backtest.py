"""通用分钟入场、次日早盘清仓及确认停牌后复牌卖出的研究现金账本。"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from datetime import time as clock

import polars as pl

from app.backtest.costs import buy_quantity_rules
from app.backtest.engine import BacktestEngine, MatcherConfig, TradeRecord
from app.backtest.entry_execution import (
    execute_entry_window,
    expected_trading_minutes,
    minute_capacity,
)
from app.backtest.governance import research_preflight, status_quality
from app.backtest.minute_replay import (
    MinuteSignalReplayer,
    _scalar_limit_up_price,
    minute_panel_start,
    minute_replay_feature_plan,
    requires_full_pool,
    requires_s03_scoring,
)
from app.instrument_suspension import STATUS_COLUMN, dated_status, holding_exit_status
from app.market_time import published_trading_day
from app.price_limits import is_risk_warning_name, price_limit_pct
from app.strategy.morning_liquidation import evaluate, parameters
from app.strategy.overnight_runtime import next_market_day
from app.strategy.risk_policy import price_exit, strategy_policy, valid_price

EXECUTION = "generic_morning_liquidation_resume_v2"


def run_morning(service, config, strategy, params, overrides, definitions, *,
                progress_cb, cancel_event, result_policy, run_id, t0, provenance):
    from app.backtest.strategy import StrategyBacktestResult, _with_status_fields

    output = StrategyBacktestResult(
        run_id=run_id, config=service._config_to_dict(config), provenance=provenance,
    )
    positions, marks, records, equities, exposures = {}, {}, [], [], []
    cash = float(config.initial_capital)
    entry_rate = config.entry_participation_pct or 1.
    exit_rate = config.exit_participation_pct or 1.
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
                "capacity_checked": True, "volume_participation_upper_bound": max(entry_rate, exit_rate),
                "entry_participation_pct": entry_rate, "exit_participation_pct": exit_rate,
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
    staged = "amplitude_pct" not in sig_params
    if all(value is None for value in sig_params.values()):
        return finish("早盘清仓全部参数为空, 无法执行清仓收益回测")
    deadline_clock = clock.fromisoformat(sig_params["time"]) if sig_params["time"] is not None else None
    fallback_value = sig_params.get("fallback_time", sig_params["time"])
    fallback_clock = clock.fromisoformat(fallback_value) if fallback_value is not None else None
    stage_clocks = sorted({at for at in (deadline_clock, fallback_clock) if at is not None})
    # 成交验收上界与信号时钟分开; 空兜底不会成为兜底信号。
    execution_clock = (fallback_clock or clock(15)) if staged else deadline_clock
    observation_clock = stage_clocks[0] if stage_clocks else execution_clock
    risk = strategy_policy(strategy, overrides)
    risk_enabled = any(v is not None for v in asdict(risk).values())
    from app.strategy.entry_risk import EntryRiskPolicy
    if EntryRiskPolicy.from_mapping(overrides.get("entry_risk")).enabled:
        return finish("冻结日线入场风险暂不支持分钟策略")
    next_minute_exit = staged or config.exit_fill == "signal_next_minute"
    minute_low_exit = config.minute_price_rule == "high_low_v1"
    minute_exit_fill = "signal_next_minute" if next_minute_exit else "trigger_minute_close"
    final_clock = (expected_trading_minutes(
        datetime.combine(config.start, execution_clock) + timedelta(minutes=1),
    )[-1].time() if next_minute_exit and not staged else execution_clock)
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
        slippage_bps=config.slippage_bps, asset_type="stock",
    )
    sell_matcher = replace(matcher, slippage_bps=0) if next_minute_exit else matcher
    bf = service._effective_basic_filter(strategy, overrides)
    daily_bars = service.strategy_engine.minute_daily_history_bars(
        [config.strategy_id], overrides_map={config.strategy_id: overrides},
    )
    if requires_s03_scoring(service.strategy_engine, strategy, overrides):
        from app.services.selection_clock import S02_ID
        daily_bars = max(daily_bars, 65, service.strategy_engine.minute_daily_history_bars([S02_ID]))
    feature_plan = _with_status_fields(minute_replay_feature_plan(daily_bars, bf, overrides), "stock")
    if requires_s03_scoring(service.strategy_engine, strategy, overrides):
        s02 = service.strategy_engine.get(S02_ID)
        loader = service.strategy_engine._override_loader
        s02_bf = {**s02.basic_filter, **((loader(S02_ID) if loader else {}).get("basic_filter") or {})}
        source_plan = minute_replay_feature_plan(daily_bars, s02_bf)
        feature_plan = replace(feature_plan,
                               base_columns=feature_plan.base_columns | source_plan.base_columns,
                               indicator_columns=feature_plan.indicator_columns | source_plan.indicator_columns)
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
        (scoring_warmup_bars({name: 1})
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
        *issues, "原始现金账本未计未经核验的分红送转; 分钟参与率为成交容量上界",
    ]
    output.config.update(
        minute_execution=EXECUTION, minute_exit_fill=minute_exit_fill,
        volume_participation_upper_bound=max(entry_rate, exit_rate),
    )
    output.strategy_info = {
        "id": config.strategy_id, "name": strategy.meta.get("name", config.strategy_id),
        "execution_backend": "minute_filter", "minute_execution": EXECUTION,
        "entry_signals": service._effective_signals(overrides, "entry_signals", strategy.entry_signals),
        "exit_signals": exits, "exit_params": sig_params,
        "minute_entry_fill": strategy.meta.get("minute_entry_fill", "signal_close"),
        "minute_exit_fill": minute_exit_fill, "volume_participation_upper_bound": max(entry_rate, exit_rate),
        "entry_participation_pct": entry_rate, "exit_participation_pct": exit_rate,
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
    # 使用与日线回测相同的精确T-1环境过滤, 只限制新入场。
    prior_day = config.start - timedelta(days=1)
    while published_trading_day(prior_day) is False:
        prior_day -= timedelta(days=1)
    labels = tuple(map(str, [prior_day, *days]))
    try:
        from app.config import settings
        regime_mask = service._build_regime_mask(
            labels, config.regime_filter,
            getattr(getattr(repo, "store", None), "data_dir", None) or settings.data_dir,
            required_start=config.start, required_end=config.end,
        )
    except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
        return finish(f"环境过滤失败: {exc}")
    output.config["risk_policy"] = asdict(risk)
    minute_days = set(repo.list_minute_dates(config.start, config.end, "stock"))
    replayer = MinuteSignalReplayer(service.engine, service.strategy_engine)

    def market_value():
        return sum(p["remaining"] * marks[symbol] for symbol, p in positions.items())

    def fee(notional, side, day):
        return (matcher.buy_value(notional) - notional if side == "buy"
                else notional - sell_matcher.sell_value(notional, day))

    def reject_overdue(day):
        overdue = [p for p in positions.values() if p["entry_date"] < day
                   and not p.get("suspension_deferred")]
        if overdue:
            counters["deadline_violations"] += len(overdue)
            detail = "; ".join(
                f"{p['symbol']} 剩余{p['remaining']}股, 已卖{p['shares'] - p['remaining']}/{p['shares']}股"
                for p in overdue
            )
            return finish(f"{day} {final_clock.isoformat()}清仓截止后仍有余股, {detail}; 保留实际成交和持仓")
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
                strict_research=config.strict_research,
            )
            if replay.selection_provenance:
                provenance.setdefault("s03_selection_replay", []).extend(replay.selection_provenance)
                output.stats["warnings"] = list(dict.fromkeys([
                    *output.stats.get("warnings", []),
                    *[warning for row in replay.selection_provenance for warning in row["warnings"]],
                ]))
            replayed += replay.replayed_days
            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")
            entry_error = ("; ".join(replay.errors) or f"{day} 分钟入场回放数据不完整") if (
                replay.errors or replay.skipped_days
            ) else None
            counters["buy_limit_up"] += replay.buy_limit_up
            counters["buy_no_next_minute"] += replay.buy_no_next_minute
            hits_at = defaultdict(list)
            entry_hits = replay.hits
            if regime_mask is not None and not regime_mask[index + 1]:
                counters["buy_regime_filter"] += len(entry_hits)
                entry_hits = []
            for hit in entry_hits:
                at = datetime.combine(day, clock.fromisoformat(hit.trigger_time))
                hits_at[(at, 0)].append(hit)
                output.signals.append({
                    "date": str(day), "symbol": hit.symbol, "side": "buy",
                    "signal_time": f"{day} {hit.signal_time or hit.trigger_time}",
                    "datetime": str(at), "price": hit.raw_entry_price, "score": hit.score,
                    "signal_bar": at.hour * 60 + at.minute, "reason": "entry",
                })
            symbols = set(positions) | {h.symbol for h in entry_hits}
            history = repo.get_minute_by_dates(sorted(symbols), [day], "stock") if symbols else pl.DataFrame()
            if not history.is_empty() and history.schema["datetime"].time_zone:
                history = history.with_columns(
                    pl.col("datetime").dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None),
                )
            groups = history.partition_by("symbol", as_dict=True) if not history.is_empty() else {}
            triggers, last_sale, suspended, sell_windows = {}, {}, set(), {}
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
                observation = datetime.combine(day, clock(15) if status == "resume" else observation_clock)
                if staged and stage_clocks and status != "resume":
                    observation -= timedelta(microseconds=1)
                evaluated = evaluate(
                    frame, sig, day=day, now=observation,
                    positions=[{**pos, "entry_date": str(pos["entry_date"]),
                                "entry_price": pos["price"],
                                "qty": pos["remaining"], "available_qty": pos["remaining"]}],
                    prev_close={symbol: prev}, suspension_history=suspension_history,
                )
                if not evaluated.hits and staged and stage_clocks and status != "resume":
                    evaluated = evaluate(
                        frame, sig, day=day, now=datetime.combine(day, observation_clock),
                        positions=[{**pos, "entry_date": str(pos["entry_date"]),
                                    "entry_price": pos["price"],
                                    "qty": pos["remaining"], "available_qty": pos["remaining"]}],
                        prev_close={symbol: prev}, suspension_history=suspension_history,
                    )
                hit = evaluated.hits[0] if evaluated.hits else None
                if risk_enabled and status != "resume" and not frame.is_empty():
                    held = sum(published_trading_day(pos["entry_date"] + timedelta(days=i)) is True
                               for i in range(1, (day - pos["entry_date"]).days + 1))
                    known_peak = pos.get("risk_peak", pos["price"])
                    for row in frame.sort("datetime").iter_rows(named=True):
                        moment = row["datetime"]
                        if moment.date() != day or not (
                            clock(9, 31) <= moment.time() <= clock(11, 30)
                            or clock(13, 1) <= moment.time() <= clock(15)
                        ):
                            continue
                        if hit and moment > datetime.fromisoformat(hit["trigger_time"]):
                            break
                        if not all(valid_price(row.get(c)) for c in ("open", "high", "low", "close")):
                            return finish(f"{day} {symbol} 风控分钟价格缺失或无效")
                        reason, threshold = price_exit(
                            risk, pos["price"], known_peak, row["open"], row["low"], row["high"],
                        )
                        if reason is None and risk.max_hold_days is not None and held >= risk.max_hold_days:
                            reason, threshold = "max_hold", row["close"]
                        if reason:
                            hit = {"symbol": symbol, "reason": reason,
                                   "trigger_time": moment.isoformat(sep=" "),
                                   "price": threshold, "signal_id": "risk_" + reason}
                            break
                        known_peak = max(known_peak, row["high"])
                if hit is None:
                    return finish(f"{day} {symbol} 无法确定早盘清仓触发")
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
                    expected_times = [moment for moment in expected_trading_minutes(
                        datetime.combine(day, clock(9, 31)), count=240) if moment <= at]
                if (expected_times and prefix.is_empty()) or (
                    (sorted(prefix["datetime"].to_list()) if not prefix.is_empty() else []) != expected_times
                ) or (
                    prefix.height and any(
                        prefix.filter(
                            ~pl.col(c).is_finite() | (pl.col(c) <= 0) | pl.col(c).is_null(),
                        ).height for c in ("high", "low", "close")
                    )
                ) or (prefix.height and prefix.filter(pl.col("high") < pl.col("low")).height):
                    return finish(f"{day} {symbol} 早盘清仓分钟前缀缺失、重复或无效")
                triggers[symbol] = hit
                last_sale[symbol] = at - timedelta(minutes=1)
                if next_minute_exit:
                    sell_windows[symbol] = expected_trading_minutes(
                        datetime.fromisoformat(hit["trigger_time"]).replace(second=0, microsecond=0)
                        + timedelta(minutes=1),
                        count=240 if staged or status == "resume" else 5,
                    )
                    if staged and status != "resume":
                        end_at = datetime.combine(day, execution_clock)
                        sell_windows[symbol] = tuple(moment for moment in sell_windows[symbol]
                                                     if moment < end_at or (
                                                         fallback_clock is None and moment == end_at))
                hit.setdefault("signal_id", exits[0])
                output.signals.append({"date": str(day), "side": "sell", **hit,
                                       "signal_bar": at.hour * 60 + at.minute})
            # 只用截至当前事件的分钟价估值与分配预算, 不借未来卖款或日终价格。
            events = defaultdict(list)
            for row in history.iter_rows(named=True):
                if row["datetime"].date() == day:
                    events[row["datetime"]].append(row)
            # 同一分钟先处理开盘成交, 再处理收盘信号; 不得预支未来卖款。
            timeline = {(at, 1) for at in events} | set(hits_at)
            if next_minute_exit:
                timeline |= {(at, 0) for at in events}
                timeline |= {(at, 0) for window in sell_windows.values() for at in window}
                timeline |= {(datetime.combine(day, final_clock), 1)}
                if not staged:
                    timeline |= {(datetime.combine(day, stage), 1) for stage in stage_clocks}
            for at, phase in sorted(timeline):
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
                    pos = positions.get(row["symbol"])
                    if (risk_enabled and phase == 1 and pos and pos["entry_date"] == day
                            and at >= pos["last_buy_fill"] and valid_price(row.get("high"))):
                        pos["risk_peak"] = max(pos["risk_peak"], row["high"])
                for symbol in list(positions):
                    trigger = triggers.get(symbol)
                    deadline = clock(15) if trigger and trigger["reason"] == "resume" else execution_clock
                    if next_minute_exit:
                        if not trigger:
                            continue
                        if phase != (1 if minute_low_exit else 0) or at not in sell_windows[symbol]:
                            continue
                    elif (phase == 0 or not trigger
                            or not datetime.fromisoformat(trigger["trigger_time"]) <= at <= datetime.combine(day, deadline)
                            or not (clock(9, 31) <= at.time() <= clock(11, 30)
                                    or clock(13, 1) <= at.time() <= clock(15))):
                        continue
                    pos = positions[symbol]
                    rows = rows_at.get(symbol, [])
                    expected_at = last_sale[symbol] + timedelta(minutes=1)
                    if expected_at.time() == clock(11, 31):
                        expected_at = datetime.combine(day, clock(13, 1))
                    if next_minute_exit and len(rows) > 1:
                        return finish(f"{day} {symbol} 清仓成交窗口重复分钟")
                    if not next_minute_exit and (len(rows) != 1 or at != expected_at):
                        return finish(f"{day} {symbol} 清仓成交窗口缺失或重复分钟")
                    last_sale[symbol] = at
                    if next_minute_exit and not rows:
                        counters["sell_missing_minutes"] += 1
                        continue
                    row = rows[0]
                    volume = row.get("volume")
                    if next_minute_exit and (not all(valid_price(row.get(c)) for c in ("open", "high", "low", "close"))
                                             or volume is None or not math.isfinite(volume) or volume < 0):
                        counters["sell_no_volume_or_price"] += 1
                        continue
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
                    price = max(down, row["low"]) if minute_low_exit else (
                        max(down, row["open"] * (1 - config.slippage_bps / 10000))
                        if next_minute_exit else row["close"])
                    if (row["high"] if next_minute_exit else price) <= down + 1e-9:
                        counters["sell_limit_down"] += 1
                        continue
                    step = buy_quantity_rules(symbol, "stock")[1] if next_minute_exit else 1
                    quantity = min(pos["remaining"], minute_capacity(row["volume"], exit_rate, step))
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
                            exit_reason=trigger["reason"] if trigger["signal_id"].startswith("risk_") else "signal",
                            shares=pos["shares"], lots=pos["shares"] / 100,
                            position_pct=pos["position_pct"], entry_value=pos["entry_value"],
                            exit_value=proceeds, pnl_amount=proceeds - pos["entry_value"],
                            entry_score=pos["score"], entry_signal_date=pos["signal_time"],
                            exit_signal_date=trigger["trigger_time"], exit_signal_id=trigger["signal_id"],
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
                if phase == 1 and at.time() >= final_clock:
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
                    previous_close = prev_rows.get(symbol, {}).get("raw_close")
                    if not valid_price(previous_close):
                        return finish(f"{day} {symbol} 缺精确昨收")
                    name = current_rows.get(symbol, {}).get("name", "")
                    value = market_value()
                    capacity = (cash + value) * config.max_exposure_pct - value
                    allocation = min(budget * weight, target, cash, capacity)
                    execution = execute_entry_window(
                        history,
                        symbol=symbol,
                        start_at=hit.entry_window_start,
                        end_at=hit.entry_window_end,
                        prior_close=float(previous_close),
                        budget=max(allocation, 0),
                        size_order=lambda price, order_budget=allocation, order_symbol=symbol: matcher.buy_quantity(
                            max(order_budget, 0), price, order_symbol,
                        ),
                        buy_value=matcher.buy_value,
                        participation_pct=entry_rate,
                        limit_up_price=_scalar_limit_up_price(
                            float(previous_close),
                            price_limit_pct(
                                symbol,
                                day,
                                is_risk_warning=bool(current_rows.get(symbol, {}).get(
                                    "is_risk_warning", is_risk_warning_name(name),
                                )),
                            ),
                        ),
                        asset_type="stock",
                    )
                    counters["entry_target_shares"] += execution.target_shares
                    counters["entry_filled_shares"] += execution.filled_shares
                    counters["entry_cancelled_shares"] += execution.cancelled_shares
                    if execution.status == "partial":
                        counters["entry_partial_orders"] += 1
                    elif execution.status == "unfilled":
                        counters["entry_unfilled_orders"] += 1
                    output.entry_orders.append({
                        "symbol": symbol,
                        "signal_time": f"{day} {hit.signal_time or hit.trigger_time}",
                        "window_start": hit.entry_window_start.isoformat(sep=" "),
                        "target_shares": execution.target_shares,
                        "filled_shares": execution.filled_shares,
                        "cancelled_shares": execution.cancelled_shares,
                        "average_price": execution.average_price,
                        "status": execution.status,
                    })
                    if execution.filled_shares <= 0 or execution.average_price is None:
                        counters["buy_lot_size"] += 1
                        continue
                    quantity = execution.filled_shares
                    price = execution.average_price
                    entry_value = execution.value
                    if entry_value > cash + 1e-8 or entry_value > capacity + 1e-8:
                        return finish("买入资金守恒校验失败")
                    cash -= entry_value
                    first_fill = execution.fills[0]
                    positions[symbol] = {
                        "symbol": symbol, "name": name, "entry_date": day, "remaining": quantity,
                        "shares": quantity, "price": price, "entry_value": entry_value,
                        "entry_fee": execution.fee,
                        "position_pct": entry_value / equity, "score": hit.score,
                        "signal_time": f"{day} {hit.signal_time or hit.trigger_time}",
                        "fill_time": first_fill.datetime.strftime("%Y-%m-%d %H:%M"),
                        "sell_notional": 0., "sell_fees": 0.,
                        "risk_peak": price, "last_buy_fill": execution.fills[-1].datetime,
                    }
                    marks[symbol] = price
                    output.fills.extend({
                        "date": str(fill.datetime.date()),
                        "datetime": fill.datetime.isoformat(sep=" "),
                        "symbol": symbol,
                        "side": "buy",
                        "shares": fill.shares,
                        "price": fill.price,
                        "notional": fill.notional,
                        "fee": fill.fee,
                        "participation": fill.participation,
                        "bar": fill.bar,
                        "minute_volume_shares": fill.minute_volume_shares,
                    } for fill in execution.fills)
            for symbol, trigger in triggers.items():
                if symbol in positions and trigger["reason"] == "resume":
                    expected_last = sell_windows[symbol][-1] if next_minute_exit else datetime.combine(day, clock(15))
                    if last_sale[symbol] != expected_last:
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
