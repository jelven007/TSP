from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest

from app.market_time import CN_TZ
from app.strategy import paper, paper_auto

DAY = date(2026, 10, 19)
SYMBOL = "600000.SH"
OTHER = "000001.SZ"


@pytest.fixture
def account(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(
        paper,
        "cn_now",
        lambda: datetime.combine(paper.cn_today(), time(14), CN_TZ),
    )
    monkeypatch.setattr(paper_auto, "cn_now", lambda: paper.cn_now())
    paper.create_account(tmp_path, 100_000)
    return tmp_path


def _rule(storage, strategy_id: str, **overrides) -> dict:
    return paper_auto.create_auto_rule(storage, {
        "name": strategy_id,
        "match_kind": "strategy",
        "match_id": strategy_id,
        "side": "auto",
        "size_mode": "fixed_amount",
        "size_value": 3_000,
        "order_type": "market",
        "cooldown_days": 0,
        "max_positions": 0,
        **overrides,
    })


def _event(
    strategy_id: str,
    event_type: str,
    *,
    symbol: str = SYMBOL,
    priority: int,
    cycle_id: str = "realtime-monitor:1",
) -> dict:
    return {
        "id": f"{cycle_id}:{strategy_id}:{event_type}:{symbol}",
        "source": "strategy",
        "strategy_id": strategy_id,
        "strategy_priority": priority,
        "strategy_cycle_id": cycle_id,
        "type": event_type,
        "symbol": symbol,
        "price": 10.0,
        "entry_policy": {
            "version": 1,
            "strategy_id": strategy_id,
            "policy": {},
            "entry_risk": {},
        },
    }


def test_same_symbol_multi_strategy_buy_creates_one_attributed_order(account) -> None:
    first = _rule(account, "s1")
    _rule(account, "s2")
    events = [
        _event("s2", "buy_signal", priority=1),
        _event("s1", "buy_signal", priority=0),
    ]

    orders = paper_auto.on_rule_events(account, events, prices={SYMBOL: 10.0})

    assert len(orders) == 1
    assert orders[0]["source"] == f"auto:{first['id']}"
    assert orders[0]["entry_policy"]["primary_strategy_id"] == "s1"
    assert orders[0]["entry_policy"]["attribution_strategy_ids"] == ["s1", "s2"]
    assert orders[0]["entry_policy"]["strategy_cycle_id"] == "realtime-monitor:1"
    assert paper_auto.load_auto_skips(account)[0]["reason"] == "portfolio_duplicate"
    assert paper_auto.on_rule_events(account, events, prices={SYMBOL: 10.0}) == []


def test_same_cycle_exit_wins_and_merges_exit_intent(account, monkeypatch) -> None:
    _rule(account, "entry_owner")
    _rule(account, "exit_peer")
    buy, error = paper.create_order(
        account,
        SYMBOL,
        "buy",
        qty=300,
        ref_price=10,
        entry_policy={
            "version": 1,
            "strategy_id": "entry_owner",
            "primary_strategy_id": "entry_owner",
            "attribution_strategy_ids": ["entry_owner"],
            "auto_rule_id": paper_auto.load_auto_rules(account)[0]["id"],
            "policy": {},
            "entry_risk": {},
        },
    )
    assert error is None
    assert len(paper.evaluate_intraday(account, {SYMBOL: 10})) == 1
    assert paper.get_order(account, buy["id"])["status"] == "filled"
    monkeypatch.setattr(paper, "cn_today", lambda: DAY + timedelta(days=1))

    events = [
        _event("entry_owner", "buy_signal", priority=0),
        _event("entry_owner", "sell_signal", priority=0),
        _event("exit_peer", "pool_exit", priority=1),
    ]
    orders = paper_auto.on_rule_events(account, events, prices={SYMBOL: 10.0})

    assert len(orders) == 1
    assert orders[0]["side"] == "sell"
    active = paper_auto.load_exit_intents(account, active_only=True)
    assert len(active) == 1
    assert active[0]["event"]["primary_strategy_id"] == "entry_owner"
    assert active[0]["event"]["attribution_strategy_ids"] == ["entry_owner", "exit_peer"]
    assert any(
        row["reason"] == "portfolio_conflict_exit"
        for row in paper_auto.load_auto_skips(account)
    )


def test_account_budget_uses_most_conservative_group_limit(account) -> None:
    _rule(
        account,
        "s1",
        size_value=50_000,
        max_total_exposure_pct=80,
    )
    _rule(
        account,
        "s2",
        size_value=50_000,
        max_total_exposure_pct=5,
    )

    orders = paper_auto.on_rule_events(account, [
        _event("s1", "buy_signal", symbol=SYMBOL, priority=0),
        _event("s2", "buy_signal", symbol=OTHER, priority=1),
    ], prices={SYMBOL: 10.0, OTHER: 10.0})

    assert len(orders) == 1
    budget = paper.portfolio_budget(
        account,
        prices={SYMBOL: 10.0, OTHER: 10.0},
    )
    assert budget["exposure"] <= budget["equity"] * 0.05 + 1e-6


def test_group_budget_combines_conservative_slot_and_symbol_limits(account) -> None:
    _rule(
        account,
        "s1",
        size_value=50_000,
        max_positions=1,
        max_symbol_exposure_pct=80,
    )
    _rule(
        account,
        "s2",
        size_value=50_000,
        max_positions=5,
        max_symbol_exposure_pct=5,
    )

    orders = paper_auto.on_rule_events(account, [
        _event("s1", "buy_signal", symbol=SYMBOL, priority=0),
        _event("s2", "buy_signal", symbol=OTHER, priority=1),
    ], prices={SYMBOL: 10.0, OTHER: 10.0})

    assert len(orders) == 1
    assert orders[0]["symbol"] == SYMBOL
    budget = paper.portfolio_budget(
        account,
        prices={SYMBOL: 10.0, OTHER: 10.0},
    )
    assert budget["pending"][SYMBOL] <= budget["equity"] * 0.05 + 1e-6
    assert paper_auto.load_auto_skips(account)[0]["reason"] == "max_positions"


def test_partial_group_cycle_is_fail_closed_for_entries(account) -> None:
    _rule(account, "s1")
    event = {
        **_event("s1", "buy_signal", priority=0),
        "strategy_cycle_state": "partial",
    }

    assert paper_auto.on_rule_events(
        account,
        [event],
        prices={SYMBOL: 10.0},
    ) == []
    assert paper_auto.load_auto_skips(account)[0]["reason"] == (
        "portfolio_cycle_not_succeeded"
    )


def test_grouped_strategy_buy_does_not_add_to_held_or_pending_symbol(account) -> None:
    _rule(account, "s1")
    manual, error = paper.create_order(account, SYMBOL, "buy", qty=100, ref_price=10)
    assert error is None

    assert paper_auto.on_rule_events(
        account,
        [_event("s1", "buy_signal", priority=0)],
        prices={SYMBOL: 10.0},
    ) == []
    assert [order["id"] for order in paper.load_orders(account)] == [manual["id"]]
    assert paper_auto.load_auto_skips(account)[0]["reason"] == "portfolio_existing_symbol"
