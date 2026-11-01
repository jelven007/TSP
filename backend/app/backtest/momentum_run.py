"""两档毛止盈共用单日内核的连续分钟回测。"""
import time
from dataclasses import replace
from datetime import date, timedelta

import polars as pl

from app.backtest.engine import BacktestEngine, TradeRecord
from app.backtest.minute_replay import minute_panel_start, minute_replay_feature_plan
from app.strategy.engine import StrategyDataContext
from app.strategy.momentum_day import run_day
from app.strategy.momentum_rules import parameters
from app.strategy.momentum_selection import daily_context


def prepare_daily(panel, repo, *, as_of=None):
    """标准仓库数据按日除权、公告股本解析, 不使用历史报告冻结特征。"""
    from app.share_capital import apply_historical_float_shares, load_share_history

    panel = panel.drop("reference_close", "day_return", "previous_return",
                       "prev_d_amount20", "previous_row_date", "before_previous_row_date", strict=False)
    if as_of is not None and not panel.is_empty():
        present = panel.filter(pl.col("date") == as_of)["symbol"]
        missing = panel.filter(pl.col("date") < as_of).sort("symbol", "date").group_by(
            "symbol", maintain_order=True).tail(1).filter(~pl.col("symbol").is_in(present.to_list()))
        if not missing.is_empty():
            missing = missing.with_columns(pl.lit(as_of).alias("date"), *[
                pl.lit(None).cast(panel.schema[key]).alias(key)
                for key in ("open", "high", "low", "close", "raw_open", "raw_high",
                            "raw_low", "raw_close", "amount", "volume", "ex_factor")
                if key in panel.columns])
            panel = pl.concat([panel, missing], how="vertical_relaxed")
    if "float_shares" not in panel.columns:
        instruments = repo.get_instruments_asset("stock")
        panel = panel.join(instruments.select("symbol", "float_shares"), on="symbol", how="left")
    panel = apply_historical_float_shares(
        panel, load_share_history(repo.store.data_dir), today=date.max)
    # 原始分钟价的前收盘参考按该日ex_factor调整。
    factor_rows = repo.execute_all(
        "SELECT symbol,trade_date AS date,ex_factor FROM adj_factor WHERE trade_date BETWEEN ? AND ?",
        [panel["date"].min(), panel["date"].max()],
    )
    factors = pl.DataFrame(factor_rows, schema={"symbol": pl.String, "date": pl.Date, "ex_factor": pl.Float64},
                           orient="row")
    if "ex_factor" not in panel.columns:
        panel = panel.join(factors, on=["symbol", "date"], how="left") if not factors.is_empty() else panel.with_columns(pl.lit(1.).alias("ex_factor"))
    panel = daily_context(panel)
    raw = "raw_close" if "raw_close" in panel.columns else "close"
    panel = panel.with_columns(
        (pl.col(raw).shift(1).over("symbol") / pl.col("ex_factor").fill_null(1.)).alias("reference_close")
    ).with_columns(
        (pl.col(raw) / pl.col("reference_close") - 1).alias("day_return")
    ).with_columns(pl.col("day_return").shift(1).over("symbol").alias("previous_return"))
    return panel


def run_momentum(service, config, strategy, values, overrides, *,
                 progress_cb, cancel_event, result_policy, run_id, t0, provenance):
    from app.backtest.strategy import StrategyBacktestResult
    from app.market_time import published_trading_day

    params = parameters(values)
    output = StrategyBacktestResult(
        run_id=run_id, config={**service._config_to_dict(config), "params": params,
            "initial_capital": params["initial_cash"], "max_positions": params["position_limit"],
            "commission_pct": params["commission"], "minimum_commission": params["minimum_commission"],
            "transfer_fee_pct": params["transfer_fee"], "stamp_tax_pct": params["stamp_tax"],
            "slippage_bps": params["slippage"] * 10000, "minute_execution": "momentum_capacity_v1"},
        provenance=provenance, strategy_info={"id": strategy.meta["id"], "name": strategy.meta["name"],
            "params": params, "minute_execution": "momentum_capacity_v1", "execution_backend": "minute_filter"})
    state = {"cash": params["initial_cash"], "positions": []}
    curves, decisions, suspensions, failures = [], [], [], []

    def finish(error=None):
        output.error = error
        output.elapsed_ms = (time.perf_counter() - t0) * 1000
        output.stats.update({
            "minute_execution": "momentum_capacity_v1", "certified": False,
            "result_scope": "raw_price_cash_ledger_excluding_unverified_dividends",
            "warnings": ["指定3052成员池属于研究池快照; 完整历史证券状态及现金分红权益未经认证"],
            "execution": {"cash": state["cash"], "remaining_positions": state["positions"],
                "entry_decisions": decisions, "suspensions": suspensions, "failures": failures,
                "deadline_violations": len(failures), "capacity_checked": True,
                "aggregated_order_fees": True, "buy_fills": sum(f["side"] == "buy" for f in output.fills),
                "sell_fills": sum(f["side"] == "sell" for f in output.fills)}})
        if error:
            output.stats["status"] = "rejected_before_NAV"
            output.stats["execution"]["partial_equity_curve"] = curves
        return output

    if config.mode != "position" or config.asset_type != "stock":
        return finish("动量容量策略仅支持沪深A股连续组合模式")
    if config.regime_filter or any(overrides.get(k) for k in (
        "overlay_filter", "scoring", "entry_signals", "exit_signals", "entry_risk", "stop_loss",
        "take_profit", "trailing_stop", "trailing_take_profit_activate", "trailing_take_profit_drawdown")):
        return finish("动量容量策略请通过策略自身参数配置选股、退出、资金和费用")
    repo = service.engine.repo
    try:
        plan = minute_replay_feature_plan(25)
        # S02 uses raw prices, not daily limit signals; omit their required columns too.
        plan = replace(plan, base_columns=plan.base_columns - plan.signal_columns,
                       signal_columns=frozenset())
        panel = service.engine.load_panel_for_backtest(
            None, minute_panel_start(config.start, 25), config.end,
            plan, asset_type="stock")
        if panel.is_empty():
            return finish("缺少回测日线行情")
        panel = prepare_daily(panel, repo)
        axis = sorted(panel["date"].unique().to_list())
        days = []
        for n in range((config.end - config.start).days + 1):
            day = config.start + timedelta(days=n)
            known = published_trading_day(day)
            if known is None:
                return finish(f"{day}缺少已发布交易日历")
            if known:
                if day not in axis:
                    return finish(f"{day}缺少交易日日线")
                days.append(day)
        minute_days = set(repo.list_minute_dates(config.start, config.end, "stock"))
        universe = panel["symbol"].unique().to_list()
        for i, day in enumerate(days):
            if cancel_event is not None and cancel_event.is_set():
                return finish("cancelled")
            if progress_cb:
                progress_cb({"day": i + 1, "total": len(days), "date": str(day)})
            if day not in minute_days:
                return finish(f"{day}缺少分钟分区")
            frame = repo.get_minute_by_dates(universe, [day], "stock")
            if frame.is_empty():
                return finish(f"{day}分钟数据为空")
            context = StrategyDataContext("stock", "1m", day, history=frame,
                daily_history=panel.filter(pl.col("date") <= day))
            selected = strategy.filter_minute_history_fn(frame, params, daily=context.daily_history)
            if config.symbols is not None and not selected.is_empty():
                selected = selected.filter(pl.col("symbol").is_in(config.symbols))
            result = run_day(context, params, selected, previous_state=state, allow_buy=i < len(days) - 1)
            state = result["state"]
            output.daily_results.append({k: result[k] for k in (
                "as_of", "rows", "total", "sections", "state", "portfolio_scope", "error")})
            output.trades.extend(result["trades"])
            output.fills.extend(result["fills"])
            output.signals.extend(result["signals"])
            decisions.extend(result["entry_decisions"])
            suspensions.extend(result["suspensions"])
            curves.append(result["equity"])
            if result["error"]:
                failures.append({"date": str(day), "error": result["error"]})
                return finish(result["error"])
    except (ValueError, OSError, pl.exceptions.PolarsError) as exc:
        return finish(str(exc))
    if not curves:
        return finish("区间没有交易日")
    records = [TradeRecord(symbol=t["symbol"], entry_date=date.fromisoformat(t["entry_date"][:10]),
               exit_date=date.fromisoformat(t["exit_date"][:10]), entry_price=t["entry_price"],
               exit_price=t["exit_price"], pnl_pct=t["pnl_pct"], duration=t["duration"],
               exit_reason=t["exit_reason"], entry_value=t["entry_value"], exit_value=t["exit_value"],
               pnl_amount=t["pnl_amount"]) for t in output.trades]
    output.stats.update(BacktestEngine._calc_portfolio_stats_from_values(
        [params["initial_cash"], *[e["value"] for e in curves]],
        [1 - e["cash"] / e["value"] for e in curves], records, params["initial_cash"],
        include_monte_carlo=result_policy.include_monte_carlo))
    peak, drawdowns = params["initial_cash"], []
    for e in curves:
        peak = max(peak, e["value"])
        drawdowns.append({"date": e["date"], "value": e["value"] / peak - 1})
    output.stats.update(total_return=curves[-1]["value"] / params["initial_cash"] - 1,
        annual_return=(curves[-1]["value"] / params["initial_cash"]) ** (252 / len(curves)) - 1,
        max_drawdown=min(d["value"] for d in drawdowns), execution_backend="minute_filter")
    output.equity_curve = curves if result_policy.include_curves else []
    output.drawdown_curve = drawdowns if result_policy.include_curves else []
    return finish()
