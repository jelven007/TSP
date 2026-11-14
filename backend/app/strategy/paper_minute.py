"""S02 capacity execution inside the existing account transaction and fill ledger."""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta

import polars as pl

from app.backtest.engine import MatcherConfig
from app.backtest.entry_execution import execute_entry_window, expected_trading_minutes
from app.market_time import CN_TZ
from app.multitenancy.paper_storage import PaperSession, paper_lock, paper_operation
from app.services.daily_close_validation import require_final_daily_rows
from app.strategy import morning_liquidation, paper
from app.strategy.basic_filter import previous_session

MODE = "s02_minute"


def _clock(value):
    at = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(at, datetime):
        raise ValueError("缺少S02信号时钟")
    return at.astimezone(CN_TZ).replace(tzinfo=None) if at.tzinfo else at


def _ceil_minute(at):
    base = at.replace(second=0, microsecond=0)
    return base + timedelta(minutes=1) if at != base else base


def matcher_for_account(acc):
    return MatcherConfig(
        commission_pct=float(acc["commission_pct"]),
        minimum_commission=float(acc["min_commission"]),
        stamp_tax_pct=float(acc["stamp_tax_pct"]) if acc.get("stamp_tax_mode", "fixed") == "fixed" else None,
        stamp_tax_mode=acc.get("stamp_tax_mode", "fixed"),
        transfer_fee_pct=float(acc.get("transfer_fee_pct", 0)),
        slippage_bps=float(acc["slippage_bps"]), asset_type="stock",
    )


def _cost_snapshot(acc):
    return {key: acc[key] for key in (
        "commission_pct", "min_commission", "stamp_tax_pct", "stamp_tax_mode",
        "transfer_fee_pct", "slippage_bps",
    ) if key in acc}


def _previous(repo, day, symbols):
    expected = previous_session(day)
    raw, _ = repo.get_daily_close_inputs(expected, include_closing_minute=False)
    if "symbol" not in raw.columns:
        raise ValueError("缺少精确前交易日原始日K")
    raw = raw.filter(pl.col("symbol").is_in(symbols))
    require_final_daily_rows(raw, expected)
    return {row["symbol"]: float(row["close"]) for row in raw.iter_rows(named=True)}


def _frame(repo, symbols, day, through):
    frame = repo.get_minute_batch(symbols, day, "stock")
    required = {"symbol", "datetime", "open", "high", "low", "close", "volume"}
    if frame.is_empty():
        return pl.DataFrame(schema={
            "symbol": pl.String, "datetime": pl.Datetime,
            **{key: pl.Float64 for key in ("open", "high", "low", "close", "volume")},
        })
    if not required <= set(frame.columns):
        raise ValueError("S02成交分钟缺少字段")
    if frame.schema["datetime"].time_zone:
        frame = frame.with_columns(pl.col("datetime").dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None))
    for field in ("open", "high", "low", "close"):
        if f"raw_{field}" in frame.columns:
            frame = frame.with_columns(pl.col(f"raw_{field}").alias(field))
    return frame.filter(
        pl.col("symbol").is_in(symbols) & (pl.col("datetime").dt.date() == day)
        & (pl.col("datetime") <= through)
    ).sort(["symbol", "datetime"])


def create_entry(data, rule, event, repo, now, account_id=paper.DEFAULT_ACCOUNT_ID, *, prices=None):
    """Freeze the signal's window and budget; never fall back to a quote order."""
    try:
        if repo is None:
            raise ValueError("S02分钟数据服务尚未接入")
        now = _clock(now)
        policy = dict(event.get("entry_policy") or {})
        if policy.get("strategy_id") != "custom_mvxxcp88" or policy.get("as_of") != str(now.date()):
            raise ValueError("S02入场政策或交易日不匹配")
        signal = _clock(policy.get("signal_datetime"))
        available = _clock(policy.get("entry_evaluated_at"))
        if signal.date() != now.date() or not signal <= available <= now:
            raise ValueError("S02信号时钟无效")
        start = _ceil_minute(max(signal + timedelta(minutes=1), available))
        window = expected_trading_minutes(start)
        if not window or now > window[-1] or now.time() > time(14, 55):
            raise ValueError("S02新入场或五分钟成交窗口已截止")
        sig = policy.get("exit_definition")
        if not isinstance(sig, dict) or sig.get("template") != morning_liquidation.TEMPLATE:
            raise ValueError("缺少S02早盘清仓定义")
        morning_liquidation.parameters(sig)
        acc = paper.get_account(data, account_id)
        matcher = matcher_for_account(acc)
        budget = paper.portfolio_budget(data, account_id, prices=prices)
        symbol = event["symbol"]
        amount = (rule["size_value"] if rule["size_mode"] == "fixed_amount"
                  else budget["equity"] * rule["size_value"] / 100)
        amount = min(float(amount), budget["available_cash"])
        for key, used in (
            ("max_symbol_exposure_pct", budget["holdings"].get(symbol, 0) + budget["pending"].get(symbol, 0)),
            ("max_total_exposure_pct", budget["exposure"]),
        ):
            if rule.get(key):
                amount = min(amount, max(0., budget["equity"] * rule[key] / 100 - used))
        prior = _previous(repo, now.date(), [symbol]).get(symbol)
        if not paper.valid_price(prior):
            raise ValueError("缺少精确前交易日原始收盘价")
        price = float((prices or {}).get(symbol, event["price"]))
        qty = matcher.buy_quantity(amount, price, symbol)
        policy.update(auto_rule_id=rule["id"], execution_mode=MODE)
        order, error = paper.create_order(
            data, symbol, "buy", account_id=account_id, qty=qty, ref_price=price,
            source=f"auto:{rule['id']}", entry_policy=policy, order_type="market",
            risk_limits={key: rule.get(key, 0) for key in ("max_symbol_exposure_pct", "max_total_exposure_pct")},
            valuation_prices=prices,
        )
        if order is None:
            return None, error
        order["strategy_execution"] = {
            "mode": MODE, "start_at": start.isoformat(), "end_at": window[-1].isoformat(),
            "not_before": _ceil_minute(now).isoformat(), "budget": amount,
            "prior_close": prior, "costs": _cost_snapshot(acc), "target_fixed": False,
            "rule_signature": [rule["match_id"], rule["side"], rule["execution_mode"]],
        }
        paper.save_order(data, order, account_id)
        return order, None
    except (ValueError, TypeError, KeyError, pl.exceptions.PolarsError) as exc:
        return None, str(exc)


def _sync(data, order, account_id):
    """Recover derived order/account state after an ambiguous file write."""
    fills = [f for f in paper.load_fills(data, account_id)
             if f.get("order_id") == order["id"] and f.get("execution_id")]
    order["filled_qty"] = sum(f["qty"] for f in fills)
    order["fees"] = sum(f["fee"] for f in fills)
    notional = sum(f["qty"] * f["price"] for f in fills)
    order["filled_value"] = notional + order["fees"] if order["side"] == "buy" else notional - order["fees"]
    order["fill_price"] = notional / order["filled_qty"] if order["filled_qty"] else None
    if fills:
        if order["side"] == "buy" and not order["strategy_execution"]["target_fixed"]:
            order["qty"] = fills[0]["target_qty"]
            order["strategy_execution"]["target_fixed"] = True
        order["filled_at"] = fills[-1]["ts"]
        if not paper.remaining_qty(order) and order["status"] == "pending":
            order["status"] = "filled"
        acc = paper.get_account(data, account_id)
        acc["cash"] = paper.replay_positions(data, account_id=account_id)[1]
        paper.save_account(data, acc, account_id)
        paper._materialize(data, account_id)
    paper.save_order(data, order, account_id)
    return fills


def _commit(data, order, moment, qty, price, fee, account_id):
    if qty <= 0 or not all(math.isfinite(v) for v in (price, fee)) or price <= 0 or fee < -1e-8:
        raise ValueError("S02成交分片无效")
    acc = paper.get_account(data, account_id)
    if acc.get("status") != "active" or qty > paper.remaining_qty(order):
        return False
    from app.strategy import paper_actions

    report = paper_actions.advance(data, str(moment.date()), account_id)
    if any(row["symbol"] == order["symbol"] for row in report["blocked"]):
        return False
    known = any(row["symbol"] == order["symbol"] and row["ex_date"] == str(moment.date())
                and not row.get("reason") for row in report["events"])
    if paper._factor_on(data, order["symbol"], "stock", str(moment.date())) not in (None, 1.) and not known:
        return False
    acc = paper.get_account(data, account_id)
    if order["side"] == "buy":
        budget = paper.portfolio_budget(data, account_id, prices={order["symbol"]: price}, exclude_order=order["id"])
        if qty * price + fee > budget["available_cash"] + 1e-6:
            return False
        budget["equity"] -= fee
        if paper.exposure_error(budget, order["symbol"], qty * price, order.get("risk_limits") or {}):
            return False
    else:
        pos = paper.load_positions(data, account_id).get(order["symbol"])
        if not pos or paper._available_of(pos, str(moment.date())) < qty:
            return False
    execution_id = f"{order['id']}:{moment.isoformat()}"
    if any(f.get("execution_id") == execution_id for f in paper.load_fills(data, account_id)):
        return True
    fill = {
        "seq": int(moment.replace(tzinfo=CN_TZ).timestamp() * 1000),
        "ts": paper._now_iso(), "date": str(moment.date()), "order_id": order["id"],
        "symbol": order["symbol"], "asset_type": "stock", "side": order["side"],
        "qty": qty, "price": price, "fee": max(0., fee), "kind": "fill",
        "execution_id": execution_id, "minute_datetime": moment.isoformat(),
        "target_qty": order["qty"], "execution_mode": MODE,
    }
    if order["side"] == "buy":
        fill["entry_policy"] = order["entry_policy"]
    paper._append_fill(data, fill, account_id)
    _sync(data, order, account_id)
    if isinstance(data, PaperSession):
        data.alert({**paper._fill_event(fill, account_id), "id": f"fill:{account_id}:{execution_id}"})
    return True


def _buy(data, order, frame, now, account_id):
    meta = order["strategy_execution"]
    if now.replace(second=0, microsecond=0) > _clock(meta["end_at"]):
        order.update(status="cancelled", reason=f"S02五分钟窗口结束, 撤销余量{paper.remaining_qty(order)}股")
        paper.save_order(data, order, account_id)
        return
    start, not_before = _clock(meta["start_at"]), _clock(meta["not_before"])
    matcher = matcher_for_account(meta["costs"])
    eligible = frame.filter((pl.col("symbol") == order["symbol"]) & (pl.col("datetime") >= not_before))
    available = set(eligible["datetime"])
    through = None
    # A wall-clock poll proves no bar was received. Only an observed contiguous
    # prefix can advance the checkpoint without treating normal latency as a revision.
    for moment in expected_trading_minutes(start):
        if moment < not_before:
            continue
        if moment > now or moment not in available:
            meta["waiting_for_minute"] = moment.isoformat()
            break
        meta.pop("waiting_for_minute", None)
        through = moment
    eligible = eligible.head(0) if through is None else eligible.filter(pl.col("datetime") <= through)
    plan = execute_entry_window(
        eligible, symbol=order["symbol"], start_at=start, prior_close=meta["prior_close"],
        budget=meta["budget"],
        size_order=lambda price: order["qty"] if meta["target_fixed"] else matcher.buy_quantity(meta["budget"], price, order["symbol"]),
        buy_value=matcher.buy_value,
    )
    existing = _sync(data, order, account_id)
    if not meta["target_fixed"] and plan.target_shares:
        order["qty"] = plan.target_shares
        meta["target_fixed"] = True
        paper.save_order(data, order, account_id)
    by_time = {f["minute_datetime"]: f for f in existing}
    for fragment in plan.fills:
        previous = by_time.get(fragment.datetime.isoformat())
        if previous:
            if (previous["qty"] != fragment.shares or abs(previous["price"] - fragment.price) > 1e-9
                    or abs(previous["fee"] - fragment.fee) > 1e-7):
                paper._expire(data, order, "S02已成交分钟被修订, 停止新成交并保留账本", account_id)
                return
            continue
        if meta.get("processed_through") and fragment.datetime <= _clock(meta["processed_through"]):
            paper._expire(data, order, "S02已处理分钟出现迟到或修订成交, 停止新成交并保留账本", account_id)
            return
        if order["status"] != "pending":
            break
        if not _commit(data, order, fragment.datetime, fragment.shares, fragment.price, fragment.fee, account_id):
            paper._expire(data, order, "S02买入资金或仓位校验失败, 保留实际成交", account_id)
            return
    if through is not None:
        meta["processed_through"] = through.isoformat()
    paper.save_order(data, order, account_id)
    if through == _clock(meta["end_at"]) and order["status"] == "pending":
        order.update(status="cancelled", reason=f"S02五分钟窗口结束, 撤销余量{paper.remaining_qty(order)}股")
        paper.save_order(data, order, account_id)


def _sell(data, order, frame, now, account_id):
    meta = order["strategy_execution"]
    if now.replace(second=0, microsecond=0) > _clock(meta["end_at"]):
        missing = f", 缺失分钟{meta['waiting_for_minute']}" if meta.get("waiting_for_minute") else ""
        paper._expire(data, order, f"S02清仓截止后仍有余股{paper.remaining_qty(order)}股{missing}, 保留实际成交和持仓", account_id)
        return
    existing = _sync(data, order, account_id)
    rows = frame.filter(
        (pl.col("symbol") == order["symbol"]) & (pl.col("datetime") >= _clock(meta["not_before"]))
        & (pl.col("datetime") <= _clock(meta["end_at"]))
    )
    if rows["datetime"].n_unique() != rows.height:
        raise ValueError("S02清仓成交窗口含重复分钟")
    matcher = matcher_for_account(meta["costs"])
    done = {f["minute_datetime"] for f in existing}
    notional = sum(f["qty"] * f["price"] for f in existing)
    next_at = (_clock(meta["processed_through"]) + timedelta(minutes=1)
               if meta.get("processed_through") else _clock(meta["not_before"]))
    for row in rows.iter_rows(named=True):
        moment = row["datetime"]
        if moment.isoformat() in done or moment < next_at or order["status"] != "pending":
            continue
        if next_at.time() == time(11, 31):
            next_at = datetime.combine(now.date(), time(13, 1))
        if moment != next_at:
            meta["waiting_for_minute"] = next_at.isoformat()
            break
        meta.pop("waiting_for_minute", None)
        meta["processed_through"] = moment.isoformat()
        next_at = moment + timedelta(minutes=1)
        price, volume = row["close"], row["volume"]
        if not paper.valid_price(price) or not math.isfinite(volume) or volume < 0:
            raise ValueError("S02清仓成交分钟无效")
        _, lower = paper.limit_prices(meta["prior_close"], order["symbol"], "stock", trade_date=now.date())
        if not volume or price <= lower + 1e-9:
            continue
        qty = min(paper.remaining_qty(order), math.floor(volume * 100))
        fee_before = notional - matcher.sell_value(notional, now.date())
        after = notional + qty * price
        fee = after - matcher.sell_value(after, now.date()) - fee_before
        if _commit(data, order, moment, qty, price, fee, account_id):
            notional = after
        else:
            raise ValueError("S02清仓可卖量或公司行为校验失败")
    paper.save_order(data, order, account_id)
    if now.replace(second=0, microsecond=0) >= _clock(meta["end_at"]) and order["status"] == "pending":
        missing = f", 缺失分钟{meta['waiting_for_minute']}" if meta.get("waiting_for_minute") else ""
        paper._expire(data, order, f"S02清仓截止后仍有余股{paper.remaining_qty(order)}股{missing}, 保留实际成交和持仓", account_id)


@paper_operation(write=True)
def advance(data, repo, now, account_id=paper.DEFAULT_ACCOUNT_ID):
    from app.strategy import paper_auto

    now = _clock(now)
    created = []
    with paper_lock(data, paper.PAPER_LOCK):
        acc = paper.get_account(data, account_id)
        if not acc:
            return created
        if acc.get("status") != "active":
            for order in paper.load_orders(data, account_id):
                if order["status"] == "pending" and order.get("strategy_execution"):
                    paper.cancel_order(data, order["id"], account_id)
            return created
        rules = {r["id"]: r for r in paper_auto.load_auto_rules(data, account_id)}
        orders = paper.load_orders(data, account_id)
        positions = paper.load_positions(data, account_id)
        managed = {sym: pos for sym, pos in positions.items()
                   if pos.get("qty", 0) > 0 and (pos.get("entry_policy") or {}).get("execution_mode") == MODE}
        active = [o for o in orders if o["status"] == "pending" and o.get("strategy_execution")]
        symbols = sorted(set(managed) | {o["symbol"] for o in active})
        if not symbols:
            return created
        frame = _frame(repo, symbols, now.date(), now)
        for symbol, pos in managed.items():
            policy = pos["entry_policy"]
            rule = rules.get(policy.get("auto_rule_id"))
            if not rule or not rule.get("enabled") or rule.get("execution_mode") != MODE:
                continue
            linked = [o for o in orders if o["side"] == "sell" and o.get("strategy_execution", {}).get("cycle_id") == pos["cycle_id"]]
            if any(o["status"] == "cancelled" or _clock(o["created_at"]).date() == now.date() for o in linked):
                continue
            if any(o["status"] == "pending" and o["symbol"] == symbol for o in orders):
                continue
            try:
                previous = _previous(repo, now.date(), [symbol])
                result = morning_liquidation.evaluate(
                    frame, policy["exit_definition"], day=now.date(), now=now,
                    positions=[{**pos, "symbol": symbol, "available_qty": paper._available_of(pos, str(now.date()))}],
                    prev_close=previous, suspension_history=repo.get_suspension_history(),
                )
                if not result.hits:
                    continue
                hit = result.hits[0]
                if linked and hit["reason"] != "resume":
                    continue
                deadline = time(15) if hit["reason"] == "resume" else time.fromisoformat(
                    morning_liquidation.parameters(policy["exit_definition"])["fallback_time"])
                order, error = paper.create_order(
                    data, symbol, "sell", account_id=account_id,
                    qty=paper._available_of(pos, str(now.date())), order_type="market",
                    source=f"auto:{rule['id']}",
                )
                if order is None:
                    raise ValueError(error)
                order["strategy_execution"] = {
                    "mode": MODE, "not_before": max(_ceil_minute(now), _clock(hit["trigger_time"])).isoformat(),
                    "end_at": datetime.combine(now.date(), deadline).isoformat(),
                    "exit_reason": hit["reason"], "cycle_id": pos["cycle_id"],
                    "prior_close": previous[symbol], "costs": _cost_snapshot(acc),
                    "rule_signature": [rule["match_id"], rule["side"], rule["execution_mode"]],
                }
                paper.save_order(data, order, account_id)
                active.append(order)
                created.append(order)
            except (ValueError, KeyError, TypeError) as exc:
                paper_auto._record_skip(data, account_id, rule, {"symbol": symbol}, "s02_exit_unavailable",
                                        detail={"error": str(exc)})
        for order in sorted(active, key=paper._order_sort_key):
            meta = order["strategy_execution"]
            rule = rules.get(order["source"].removeprefix("auto:"))
            if (not rule or not rule.get("enabled") or meta["rule_signature"] !=
                    [rule["match_id"], rule["side"], rule.get("execution_mode")]):
                paper.cancel_order(data, order["id"], account_id)
                continue
            _sync(data, order, account_id)
            if order["status"] != "pending":
                continue
            try:
                if _clock(meta["not_before"]).date() != now.date():
                    paper._expire(data, order, "S02成交窗口交易日已结束, 保留实际成交和余股", account_id)
                    continue
                (_buy if order["side"] == "buy" else _sell)(data, order, frame, now, account_id)
            except (ValueError, TypeError, pl.exceptions.PolarsError) as exc:
                paper._expire(data, order, f"S02分钟执行失败: {exc}; 保留实际成交和余股", account_id)
    return created
