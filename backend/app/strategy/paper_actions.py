"""Company-action journal, using the existing paper lock, transaction and replay."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from app.multitenancy.paper_storage import market_path, paper_lock, paper_operation
from app.services.corporate_actions import load_actions

KIND = "corporate_action"
LABELS = {
    "register": "权益登记", "ex": "除权应收及送转股",
    "cash_paid": "税前红利到账", "rights_subscribed": "配股缴款",
    "rights_listed": "配股上市",
}


def event_mode(account):
    return account and account.get("corporate_action_mode", "legacy_factor") == "events"


def states(fills):
    result = {}
    for fill in fills:
        if fill.get("kind") != KIND:
            continue
        state = result.setdefault(fill["action_id"], {})
        state[fill["stage"]] = fill
    return result


def _quantity(base, ratio):
    value = Decimal(str(base)) * Decimal(str(ratio))
    if value != value.to_integral_value():
        raise ValueError("零碎股分配规则未知, 等待确切配发数量")
    return int(value)


def _money(qty, per_share):
    return float((Decimal(str(qty)) * Decimal(str(per_share))).quantize(
        Decimal(".01"), rounding=ROUND_HALF_UP,
    ))


def apply_entry(positions, fill):
    """Apply only journaled cash/shares; no source reads during replay."""
    stage = fill["stage"]
    if stage == "cash_paid":
        return fill["cash_amount"]
    if stage == "rights_subscribed":
        return -fill["cash_amount"]
    if stage == "ex" and (pos := positions.get(fill["symbol"])):
        factor = fill.get("price_factor")
        if factor and pos.get("risk_entry_price"):
            pos["risk_entry_price"] /= factor
            pos["risk_factor"] = pos.get("risk_factor", 1.0) * factor
        elif pos.get("entry_policy"):
            pos["risk_adjustment_unknown"] = True
    if stage not in {"ex", "rights_listed"} or not fill.get("qty"):
        return 0
    symbol, qty = fill["symbol"], fill["qty"]
    pos = positions.setdefault(symbol, {
        "asset_type": fill["asset_type"], "qty": 0, "avg_cost": 0.0,
        "available_qty": 0, "lots": [],
    })
    cost = pos["qty"] * pos["avg_cost"]
    if stage == "rights_listed":
        cost += fill["cash_amount"]
    if pos["qty"] == 0:
        pos["cycle_id"] = f"corporate:{fill['action_id']}"
    pos["qty"] += qty
    pos["avg_cost"] = round(cost / pos["qty"], 6)
    pos["lots"].append({
        "date": fill["date"], "qty": qty, "available_on": fill["available_on"],
    })
    return 0


def receivable_assets(fills, day, prices):
    """Unpaid gross cash and subscribed, unlisted shares are separate assets."""
    cash, shares = 0.0, {}
    for state in states(fills).values():
        register = state.get("register")
        if not register:
            continue
        event = register["event"]
        if "ex" in state and "cash_paid" not in state:
            cash += state["ex"]["cash_amount"]
        subscribed = state.get("rights_subscribed")
        if subscribed and "rights_listed" not in state:
            price = prices.get(event["symbol"]) if day >= event["ex_date"] else None
            value = (subscribed["qty"] * price if price is not None and price > 0
                     else subscribed["cash_amount"])
            shares[event["symbol"]] = shares.get(event["symbol"], 0) + value
    return cash, shares


def receivables(fills, day, prices):
    cash, shares = receivable_assets(fills, day, prices)
    return round(cash + sum(shares.values()), 2)


@paper_operation()
def status(data_dir, day, account_id="default"):
    from app.strategy import paper

    account = paper.get_account(data_dir, account_id)
    if not event_mode(account):
        return {"mode": "legacy_factor", "events": [], "blocked": []}
    fills = paper.load_fills(data_dir, account_id)
    relevant = {f["symbol"] for f in fills}
    orders = [o for o in paper.load_orders(data_dir, account_id) if o["status"] == "pending"]
    relevant.update(o["symbol"] for o in orders)
    first_activity = {}
    for item in [*fills, *orders]:
        stamp = item.get("date") or item.get("created_at", day)[:10]
        first_activity[item["symbol"]] = min(first_activity.get(item["symbol"], stamp), stamp)
    journal = states(fills)
    events, blocked = [], []
    for action in load_actions(market_path(data_dir), relevant):
        if action.ex_date.isoformat() < first_activity.get(action.symbol, day) and action.id not in journal:
            continue
        event = action.model_dump(mode="json")
        state = journal.get(action.id, {})
        reason = action.missing_evidence()
        if state.get("register") and state["register"]["event"] != event:
            reason.append("事件修订与已登记账本不一致, 需核对冲正")
        if not reason and event["record_date"] < day and "register" not in state:
            history = [fill for fill in fills if fill.get("date", "") <= event["record_date"]]
            base = paper.replay_positions(data_dir, history, account_id)[0].get(action.symbol, {}).get("qty", 0)
            try:
                _quantity(base, action.bonus_per_share)
                _quantity(base, action.rights_per_share)
            except ValueError as exc:
                reason.append(str(exc))
        # A zero registered entitlement does not need a historical correction.
        if ("ex" not in state and event["ex_date"] <= day
                and state.get("register", {}).get("base_qty", 1)
                and any(fill.get("kind", "fill") == "fill" and fill["symbol"] == action.symbol
                        and fill.get("date", "") >= event["ex_date"] for fill in fills)):
            reason.append("除权后已有成交, 需核对历史账务")
        row = {**event, "stages": list(state), "base_qty": state.get("register", {}).get("base_qty", 0),
               "subscribed_qty": state.get("rights_subscribed", {}).get("qty", 0)}
        row["can_subscribe"] = bool(
            not reason and account["status"] == "active"
            and state.get("register", {}).get("rights_qty", 0) > 0
            and "rights_subscribed" not in state and action.rights_start_date
            and event["rights_start_date"] <= day <= event["rights_end_date"]
        )
        if reason:
            row["reason"] = "、".join(reason)
            if action.ex_date.isoformat() <= day:
                blocked.append(row)
        events.append(row)
    return {"mode": "events", "events": events, "blocked": blocked}


def _append(data_dir, action, stage, day, account_id, **values):
    from app.strategy import paper

    paper._append_fill(data_dir, {
        "kind": KIND, "side": KIND, "action_id": action.id, "stage": stage,
        "label": LABELS[stage], "symbol": action.symbol, "asset_type": action.asset_type,
        "date": day, "ts": paper._now_iso(), "order_id": None,
        "seq": int(paper.cn_now().timestamp() * 1000), **values,
    }, account_id)


@paper_operation(write=True)
def advance(data_dir, day, account_id="default", *, record_close=False):
    """Recognize due actions before fills; capture today's registry only after close."""
    from app.strategy import paper

    with paper_lock(data_dir, paper.PAPER_LOCK):
        report = status(data_dir, day, account_id)
        report["posted"] = 0
        if report["mode"] != "events" or not report["events"]:
            return report
        from app.data_providers.corporate_actions import CorporateAction

        for row in report["events"]:
            if row.get("reason"):
                continue
            action = CorporateAction.model_validate({
                key: row[key] for key in CorporateAction.model_fields
            })
            event = action.model_dump(mode="json")
            fills = paper.load_fills(data_dir, account_id)
            state = states(fills).get(action.id, {})
            record_day = event["record_date"]
            if record_day > day or (record_day == day and not record_close):
                continue
            if "register" not in state:
                history = [f for f in fills if f.get("date", "") <= record_day]
                pos = paper.replay_positions(data_dir, history, account_id)[0].get(action.symbol, {})
                base_qty = pos.get("qty", 0)
                try:
                    bonus = _quantity(base_qty, action.bonus_per_share)
                    rights = _quantity(base_qty, action.rights_per_share)
                except ValueError as exc:
                    report["blocked"].append({**row, "reason": str(exc)})
                    continue
                _append(data_dir, action, "register", record_day, account_id, event=event,
                        base_qty=base_qty, bonus_qty=bonus, rights_qty=rights)
                report["posted"] += 1
                state = states(paper.load_fills(data_dir, account_id))[action.id]
            register = state["register"]
            if not register["base_qty"]:
                continue
            if event["ex_date"] <= day and "ex" not in state:
                # A late observation cannot silently rewrite later executions.
                if any(f.get("kind", "fill") == "fill" and f["symbol"] == action.symbol
                       and f.get("date", "") >= event["ex_date"] for f in fills):
                    report["blocked"].append({**row, "reason": "除权后已有成交, 需核对历史账务"})
                    continue
                previous = paper._prev_close(data_dir, action.symbol, action.asset_type, event["ex_date"])
                reference = ((previous - action.cash_per_share
                              + action.rights_per_share * (action.rights_price or 0))
                             / (1 + action.bonus_per_share + action.rights_per_share)) if previous else None
                factor = previous / reference if reference and reference > 0 else None
                _append(data_dir, action, "ex", event["ex_date"], account_id,
                        qty=register["bonus_qty"], available_on=event["bonus_listing_date"],
                        price_factor=factor,
                        cash_amount=_money(register["base_qty"], action.cash_per_share))
                report["posted"] += 1
                state = states(paper.load_fills(data_dir, account_id))[action.id]
            pay_day = event["cash_pay_date"]
            if action.cash_per_share and pay_day <= day and "ex" in state and "cash_paid" not in state:
                _append(data_dir, action, "cash_paid", pay_day, account_id,
                        cash_amount=state["ex"]["cash_amount"], tax_basis="gross_before_personal_tax")
                report["posted"] += 1
            subscribed = state.get("rights_subscribed")
            if subscribed and event["rights_listing_date"] <= day and "rights_listed" not in state:
                _append(data_dir, action, "rights_listed", event["rights_listing_date"], account_id,
                        qty=subscribed["qty"], cash_amount=subscribed["cash_amount"],
                        available_on=event["rights_listing_date"])
                report["posted"] += 1
        # The marker is written last. A crash retries replay; unchanged polls
        # do not rewrite account/positions on every market tick.
        count = sum(f.get("kind") == KIND for f in paper.load_fills(data_dir, account_id))
        account = paper.get_account(data_dir, account_id)
        if account.get("corporate_action_entries") != count:
            paper.rebuild_positions(data_dir, account_id)
            account = paper.get_account(data_dir, account_id)
            account["corporate_action_entries"] = count
            paper.save_account(data_dir, account, account_id)
        return report


@paper_operation(write=True)
def subscribe(data_dir, action_id, qty, account_id="default"):
    from app.strategy import paper

    day = paper.cn_today().isoformat()
    with paper_lock(data_dir, paper.PAPER_LOCK):
        report = advance(data_dir, day, account_id)
        if report["mode"] != "events":
            raise ValueError("存量因子账户不支持事件配股")
        if any(row["id"] == action_id for row in report["blocked"]):
            raise ValueError("公司行为证据不完整或账务待核对")
        state = states(paper.load_fills(data_dir, account_id)).get(action_id, {})
        register = state.get("register")
        if not register:
            raise ValueError("没有已登记的配股权益")
        event = register["event"]
        if isinstance(qty, bool) or not isinstance(qty, int) or not 0 < qty <= register["rights_qty"]:
            raise ValueError("配股数量超过登记权益或无效")
        if "rights_subscribed" in state:
            if state["rights_subscribed"]["qty"] != qty:
                raise ValueError("已缴款的配股数量不可改写")
            return state["rights_subscribed"]
        if not event["rights_start_date"] <= day <= event["rights_end_date"]:
            raise ValueError("不在配股缴款期间")
        account = paper.get_account(data_dir, account_id)
        if account["status"] != "active":
            raise ValueError("账户已冻结")
        amount = _money(qty, event["rights_price"])
        if paper.portfolio_budget(data_dir, account_id)["available_cash"] < amount:
            raise ValueError("配股可用资金不足")
        from app.data_providers.corporate_actions import CorporateAction

        _append(data_dir, CorporateAction.model_validate(event), "rights_subscribed",
                day, account_id, qty=qty, cash_amount=amount)
        paper.rebuild_positions(data_dir, account_id)
        return states(paper.load_fills(data_dir, account_id))[action_id]["rights_subscribed"]
