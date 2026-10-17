"""Automatic direction, liquidation and account position limits."""
from datetime import date, datetime, time, timedelta

import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto

DAY = date(2026, 10, 14)
SYM = "600519.SH"
OTHER = "000001.SZ"


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(paper.cn_today(), time(14), CN_TZ))
    monkeypatch.setattr(paper_auto, "cn_now", lambda: paper.cn_now())
    paper.create_account(tmp_path, 100000)
    return tmp_path


def make_rule(storage, **overrides):
    return paper_auto.create_auto_rule(storage, {
        "name": "策略跟单", "match_kind": "strategy", "match_id": "strategy-one",
        "side": "auto", "size_mode": "fixed_amount", "size_value": 3000,
        "order_type": "market", "cooldown_days": 5, "max_positions": 1,
        **overrides,
    })


def event(kind, symbol=SYM, **overrides):
    return {
        "source": "strategy", "strategy_id": "strategy-one", "type": kind,
        "symbol": symbol, "price": 10, **overrides,
    }


def buy_and_fill(storage, symbol=SYM, qty=700):
    order, error = paper.create_order(storage, symbol, "buy", qty=qty, ref_price=10)
    assert error is None
    assert len(paper.evaluate_intraday(storage, {symbol: 10})) == 1
    return order


@pytest.mark.parametrize("entry,exit_kind", [
    ("buy_signal", "sell_signal"), ("pool_entry", "pool_exit"),
])
def test_auto_exit_liquidates_all_and_bypasses_entry_cooldown(account, monkeypatch, entry, exit_kind):
    rule = make_rule(account)
    orders = paper_auto.on_rule_events(account, [event(entry)])
    assert len(orders) == 1 and orders[0]["side"] == "buy" and orders[0]["qty"] == 300
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper_auto.on_rule_events(account, [event(entry, price=11)]) == []
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    # An exit must not depend on the amount setting or remaining equity.
    paper_auto.save_auto_rule(account, {**rule, "size_value": 1})
    sells = paper_auto.on_rule_events(account, [event(exit_kind)])
    assert len(sells) == 1 and sells[0]["side"] == "sell" and sells[0]["qty"] == 300
    assert paper_auto.on_rule_events(account, [event(exit_kind, price=11)]) == []
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper.load_positions(account)[SYM]["qty"] == 0


def test_auto_exit_respects_t1_and_existing_pending_sells(account, monkeypatch):
    make_rule(account, cooldown_days=0)
    buy_and_fill(account)
    assert paper_auto.on_rule_events(account, [event("sell_signal")]) == []
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    sell, error = paper.create_order(account, SYM, "sell", qty=100)
    assert error is None
    assert paper_auto.on_rule_events(account, [event("sell_signal")]) == []
    paper.cancel_order(account, sell["id"])
    sells = paper_auto.on_rule_events(account, [event("sell_signal")])
    assert len(sells) == 1 and sells[0]["qty"] == 700


@pytest.mark.parametrize("kind", ["hit", "hold", "", "unknown"])
def test_auto_ignores_events_without_direction_and_empty_exit(account, kind):
    make_rule(account)
    assert paper_auto.on_rule_events(account, [event(kind), event("sell_signal")]) == []
    assert paper.load_orders(account) == []


def test_fixed_sides_and_legacy_defaults_keep_original_sizing(account, monkeypatch):
    rule = make_rule(account, side="buy", cooldown_days=0)
    # Fixed buy deliberately preserves its behavior even on an exit event.
    assert paper_auto.on_rule_events(account, [event("sell_signal")])[0]["side"] == "buy"
    paper.evaluate_intraday(account, {SYM: 10})
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    paper_auto.save_auto_rule(account, {**rule, "side": "sell", "size_value": 1000})
    sell = paper_auto.on_rule_events(account, [event("buy_signal")])[0]
    assert sell["side"] == "sell" and sell["qty"] == 100
    legacy = paper_auto.normalize_rule({"name": "旧规则", "match_id": "old"})
    assert legacy["side"] == "buy" and legacy["max_positions"] == 0


def test_limit_counts_pending_buys_all_rules_and_allows_existing_symbol(account):
    rule = make_rule(account, side="buy", cooldown_days=0)
    # Manual orders also reserve a slot, not just orders from this rule.
    pending, error = paper.create_order(account, SYM, "buy", qty=100, ref_price=10)
    assert error is None
    assert paper_auto.on_rule_events(account, [event("buy_signal", OTHER)]) == []
    assert len(paper_auto.on_rule_events(account, [event("buy_signal")])) == 1
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper_auto.on_rule_events(account, [event("buy_signal", OTHER)]) == []
    assert len(paper_auto.on_rule_events(account, [event("buy_signal")])) == 1
    paper_auto.save_auto_rule(account, {**rule, "max_positions": 0})
    assert len(paper_auto.on_rule_events(account, [event("buy_signal", OTHER)])) == 1
    assert paper.get_order(account, pending["id"])["status"] == "filled"


def test_limit_deduplicates_held_symbol_and_multiple_pending_buys(account):
    make_rule(account, side="buy", cooldown_days=0, max_positions=2)
    buy_and_fill(account)
    for _ in range(2):
        order, error = paper.create_order(account, SYM, "buy", qty=100, ref_price=10)
        assert order is not None and error is None
    assert len(paper_auto.on_rule_events(account, [event("buy_signal", OTHER)])) == 1
    assert paper_auto.on_rule_events(account, [event("buy_signal", "600000.SH")]) == []


def test_cancelled_and_zero_quantity_positions_release_slots(account, monkeypatch):
    make_rule(account, cooldown_days=0)
    buy = paper_auto.on_rule_events(account, [event("buy_signal")])[0]
    paper.cancel_order(account, buy["id"])
    buy_and_fill(account)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))
    paper_auto.on_rule_events(account, [event("sell_signal")])
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper.load_positions(account)[SYM]["qty"] == 0
    assert len(paper_auto.on_rule_events(account, [event("buy_signal", OTHER)])) == 1


def test_limit_isolated_by_account(account):
    make_rule(account, cooldown_days=0)
    paper.create_account(account, 100000, account_id="second")
    make_rule_for_second = paper_auto.create_auto_rule(
        account, {**paper_auto.load_auto_rules(account)[0], "id": "ignored"}, account_id="second",
    )
    assert make_rule_for_second["max_positions"] == 1
    assert len(paper_auto.on_rule_events(account, [event("buy_signal")])) == 1
    assert len(paper_auto.on_rule_events(account, [event("buy_signal", OTHER)], "second")) == 1


@pytest.mark.parametrize("value", [-1, True, 1.5, "2", None])
def test_invalid_position_limits_rejected(account, value):
    with pytest.raises(ValueError, match="max_positions"):
        make_rule(account, max_positions=value)


def test_auto_requires_a_strategy_source(account):
    with pytest.raises(ValueError, match="策略"):
        make_rule(account, match_kind="rule")


def next_day(monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))


def test_exit_survives_t1_restart_cancel_and_no_new_signal(account, monkeypatch):
    make_rule(account, cooldown_days=0)
    buy_and_fill(account)
    ev = event("sell_signal", id="exit-one")
    assert paper_auto.on_rule_events(account, [ev]) == []
    intents = paper_auto.load_exit_intents(account)
    assert len(intents) == 1 and intents[0]["status"] == "pending"
    assert "T+1" in intents[0]["last_error"]
    next_day(monkeypatch)
    # A fresh Path and empty events model a restarted collector without a new signal.
    sells = paper_auto.on_rule_events(type(account)(str(account)), [], prices={SYM: 10})
    assert len(sells) == 1 and sells[0]["qty"] == 700
    assert paper_auto.on_rule_events(account, [ev], prices={SYM: 10}) == []
    paper.cancel_order(account, sells[0]["id"])
    retried = paper_auto.on_rule_events(account, [], prices={SYM: 11})
    assert len(retried) == 1 and retried[0]["ref_price"] == 11
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper_auto.on_rule_events(account, [], prices={SYM: 10}) == []
    assert paper_auto.load_exit_intents(account)[0]["status"] == "completed"


def test_exit_retries_remaining_after_manual_partial_sale(account, monkeypatch):
    make_rule(account)
    buy_and_fill(account)
    paper_auto.on_rule_events(account, [event("pool_exit", id="partial")])
    next_day(monkeypatch)
    manual, error = paper.create_order(account, SYM, "sell", qty=200, ref_price=10)
    assert error is None
    assert paper_auto.on_rule_events(account, [], prices={SYM: 10}) == []
    paper.evaluate_intraday(account, {SYM: 10})
    assert paper.get_order(account, manual["id"])["status"] == "filled"
    sells = paper_auto.on_rule_events(account, [], prices={SYM: 10})
    assert len(sells) == 1 and sells[0]["qty"] == 500


@pytest.mark.parametrize("change", ["disabled", "deleted", "direction", "target", "order_type"])
def test_rule_changes_cancel_pending_exit_and_do_not_revive(account, monkeypatch, change):
    rule = make_rule(account)
    buy_and_fill(account)
    next_day(monkeypatch)
    sell = paper_auto.on_rule_events(account, [event("sell_signal", id="cancel")])[0]
    if change == "disabled":
        paper_auto.set_enabled(account, rule["id"], False)
        paper_auto.set_enabled(account, rule["id"], True)
    elif change == "deleted":
        paper_auto.delete_auto_rule(account, rule["id"])
    else:
        fields = {"direction": {"side": "buy"}, "target": {"match_id": "other"},
                  "order_type": {"order_type": "close"}}
        paper_auto.save_auto_rule(account, {**rule, **fields[change]})
    assert paper.get_order(account, sell["id"])["status"] == "cancelled"
    assert paper_auto.on_rule_events(account, [], prices={SYM: 10}) == []
    assert paper_auto.load_exit_intents(account)[0]["status"] == "cancelled"


def test_freeze_pauses_exit_and_market_retry_requires_quote(account, monkeypatch):
    make_rule(account)
    buy_and_fill(account)
    paper_auto.on_rule_events(account, [event("sell_signal", id="frozen")])
    next_day(monkeypatch)
    paper.set_frozen(account)
    assert paper_auto.on_rule_events(account, [], prices={SYM: 10}) == []
    paper.set_frozen(account, frozen=False)
    assert paper_auto.retry_exit_intents(account) == []
    assert paper_auto.on_rule_events(account, [], prices={OTHER: 10}) == []
    assert len(paper_auto.on_rule_events(account, [], prices={SYM: 10})) == 1


def test_old_exit_cannot_sell_reentered_position_even_before_matching(account, monkeypatch):
    make_rule(account)
    buy_and_fill(account)
    next_day(monkeypatch)
    sell = paper_auto.on_rule_events(account, [event("pool_exit", id="old-cycle")])[0]
    # Flatten through another ledger entry, then buy the same security again.
    manual = {**sell, "id": "manual-flatten", "source": "manual", "status": "filled"}
    paper._append_fill(account, {
        "kind": "fill", "order_id": manual["id"], "symbol": SYM, "side": "sell",
        "qty": 700, "price": 10., "fee": 5., "date": paper.cn_today().isoformat(),
        "ts": 1, "asset_type": "stock",
    })
    paper._materialize(account)
    buy, error = paper.create_order(account, SYM, "buy", qty=200, ref_price=10)
    assert error is None
    # Model a fill arriving from settlement; do not match the stale sell first.
    with paper.PAPER_LOCK:
        assert paper._fill_order(account, buy, 10., paper.cn_today().isoformat()) is not None
    paper_auto.retry_exit_intents(account, submit=False)
    assert paper.get_order(account, sell["id"])["status"] == "cancelled"
    assert paper_auto.load_exit_intents(account)[0]["status"] == "cancelled"
    assert paper_auto.on_rule_events(account, [], prices={SYM: 10}) == []


def test_queued_exit_retries_without_live_quotes(account, monkeypatch):
    make_rule(account, order_type="next_open")
    buy_and_fill(account)
    paper_auto.on_rule_events(account, [event("pool_exit", id="queued")])
    next_day(monkeypatch)
    sells = paper_auto.retry_exit_intents(account)
    assert len(sells) == 1 and sells[0]["order_type"] == "next_open"
    expired = {**sells[0], "status": "expired", "reason": "无行情"}
    paper.save_order(account, expired)
    assert len(paper_auto.retry_exit_intents(account)) == 1


def test_live_exit_blocks_new_auto_buy_for_same_position(account, monkeypatch):
    make_rule(account, cooldown_days=0)
    buy_and_fill(account)
    assert paper_auto.on_rule_events(account, [
        event("buy_signal", id="contradictory-buy"), event("sell_signal", id="exit-first"),
    ]) == []
    next_day(monkeypatch)
    sells = paper_auto.on_rule_events(account, [event("buy_signal", id="later-buy")], prices={SYM: 10})
    assert len(sells) == 1 and sells[0]["side"] == "sell"


def test_file_exit_recovers_crash_between_order_and_intent_update(account, monkeypatch):
    make_rule(account)
    buy_and_fill(account)
    next_day(monkeypatch)
    original = paper_auto._put_record

    def crash(storage, kind, key, value, account_id):
        if kind == "exit" and value["status"] == "submitted":
            raise RuntimeError("crash after order write")
        return original(storage, kind, key, value, account_id)

    monkeypatch.setattr(paper_auto, "_put_record", crash)
    with pytest.raises(RuntimeError, match="crash after order"):
        paper_auto.on_rule_events(account, [event("sell_signal", id="file-crash")])
    assert len(paper.load_orders(account)) == 2  # buy + one durably linked sell
    monkeypatch.setattr(paper_auto, "_put_record", original)
    assert paper_auto.on_rule_events(account, [], prices={SYM: 10}) == []
    assert paper_auto.load_exit_intents(account)[0]["status"] == "submitted"
    assert len(paper.load_orders(account)) == 2


def test_expired_exit_waits_while_limit_down_persists(account, monkeypatch):
    make_rule(account)
    buy_and_fill(account)
    next_day(monkeypatch)
    monkeypatch.setattr(paper, "_prev_close", lambda *_: 10.)
    sell = paper_auto.on_rule_events(account, [event("pool_exit", id="limit-down")])[0]
    assert paper.evaluate_intraday(account, {SYM: 9.}) == []
    assert paper.get_order(account, sell["id"])["status"] == "expired"
    for _ in range(3):
        assert paper_auto.on_rule_events(account, [], prices={SYM: 9.}) == []
    assert len(paper.load_orders(account)) == 2
    assert "跌停" in paper_auto.load_exit_intents(account)[0]["last_error"]
    assert len(paper_auto.on_rule_events(account, [], prices={SYM: 10.})) == 1


def test_freeze_also_pauses_already_submitted_exit(account, monkeypatch):
    make_rule(account)
    buy_and_fill(account)
    next_day(monkeypatch)
    sell = paper_auto.on_rule_events(account, [event("sell_signal", id="freeze-pending")])[0]
    paper.set_frozen(account)
    assert paper.evaluate_intraday(account, {SYM: 10.}) == []
    assert paper.get_order(account, sell["id"])["status"] == "pending"
    paper.set_frozen(account, frozen=False)
    assert len(paper.evaluate_intraday(account, {SYM: 10.})) == 1


@pytest.mark.parametrize("mode,value,pending,expected", [
    ("fixed_amount", 10_000_000, 0, 500),
    ("fixed_amount", 10_000_000, 200, 300),
    ("fixed_amount", 1000, 200, 100),
    ("pct_equity", 100, 200, 300),
    ("fixed_amount", 10_000_000, 500, 0),
])
def test_fixed_sell_caps_to_remaining_available(
    account, monkeypatch, mode, value, pending, expected,
):
    make_rule(account, side="sell", size_mode=mode, size_value=value, cooldown_days=0)
    buy_and_fill(account, qty=500)
    next_day(monkeypatch)
    if pending:
        _, error = paper.create_order(account, SYM, "sell", qty=pending, ref_price=10)
        assert error is None
    ev = event("sell_signal", id="fixed-sell")
    orders = paper_auto.on_rule_events(account, [ev, ev])
    assert [order["qty"] for order in orders] == ([expected] if expected else [])
    assert paper_auto.on_rule_events(account, [ev]) == []
    assert sum(
        o["qty"] for o in paper.load_orders(account)
        if o["side"] == "sell" and o["status"] == "pending"
    ) == pending + expected
    assert paper_auto.load_exit_intents(account) == []
    if not expected:
        skipped = paper_auto.load_auto_skips(account)[0]
        assert skipped["reason"] == "no_sellable_qty"
        assert "可卖数量为 0" in skipped["message"]


def test_fixed_sell_excludes_t1_and_other_accounts(account, monkeypatch):
    make_rule(account, side="sell", size_value=10_000_000, cooldown_days=0)
    buy_and_fill(account, qty=500)
    assert paper_auto.on_rule_events(account, [event("sell_signal")]) == []
    assert paper_auto.load_auto_skips(account)[0]["reason"] == "no_sellable_qty"
    next_day(monkeypatch)
    buy_and_fill(account, qty=200)
    paper.create_account(account, 100000, account_id="second")
    rule = paper_auto.load_auto_rules(account)[0]
    paper_auto.create_auto_rule(account, rule, account_id="second")
    assert paper_auto.on_rule_events(account, [event("sell_signal")], "second") == []
    sells = paper_auto.on_rule_events(account, [event("sell_signal")])
    assert [o["qty"] for o in sells] == [500]
