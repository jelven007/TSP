"""Original full paper domain on real PostgreSQL: ledger, isolation and recovery."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.api.paper import router
from app.cell.context import AccessError
from app.market_time import CN_TZ
from app.multitenancy import access
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.paper_storage import PaperSession, TenantPaper
from app.strategy import paper, paper_auto

DAY = date(2026, 10, 14)
NEXT = DAY + timedelta(days=1)
SYM = "600519.SH"


@pytest.fixture
def owned(env, tmp_path, monkeypatch):
    _, first = register(env)
    _, second = register(env)
    own = TenantPaper(env["db"], env["identity"].context(first, None, "paper"), tmp_path)
    other = TenantPaper(env["db"], env["identity"].context(second, None, "other-paper"), tmp_path)
    monkeypatch.setattr(paper, "cn_today", lambda: DAY)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(DAY, time(14, 0), tzinfo=CN_TZ))
    monkeypatch.setattr(paper_auto, "cn_now", lambda: paper.cn_now())
    base = tmp_path / "kline_daily"
    base.mkdir()
    pl.DataFrame({
        "symbol": [SYM] * 3, "date": [DAY - timedelta(days=1), DAY, NEXT],
        "open": [10., 10.2, 10.5], "close": [10., 10.8, 10.9],
    }).write_parquet(base / "bars.parquet")
    # Any attempt to read or write old shared private files is a failure.
    monkeypatch.setattr(paper, "_root", lambda *a: pytest.fail("legacy paper directory"))
    return own, other


def assert_ledger(storage):
    positions, cash = paper.replay_positions(storage)
    assert paper.get_account(storage)["cash"] == cash
    materialized = paper.load_positions(storage)
    for symbol, position in positions.items():
        assert materialized[symbol]["qty"] == position["qty"]
        assert materialized[symbol]["avg_cost"] == position["avg_cost"]
    assert cash >= 0


def test_event_cash_rights_concurrency_isolation_and_transaction_recovery(owned, monkeypatch):
    from app.data_providers.corporate_actions import CorporateAction
    from app.services.corporate_actions import save_actions
    from app.strategy import paper_actions

    own, other = owned
    for storage in (own, other):
        paper.create_account(storage, 100_000, slippage_bps=0)
    paper.create_order(own, SYM, "buy", qty=1000, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    cash_before = paper.get_account(own)["cash"]
    event = CorporateAction(
        id="verified", symbol=SYM, source="fixture", record_date=DAY, ex_date=NEXT,
        cash_per_share=1, cash_pay_date=NEXT, dates_verified=True, evidence="fixture",
        rights_per_share=.2, rights_price=5, rights_start_date=NEXT,
        rights_end_date=NEXT, rights_listing_date=NEXT + timedelta(days=1),
    )
    save_actions(own.public_data_dir, [event])
    original = paper.rebuild_positions
    monkeypatch.setattr(paper, "rebuild_positions", lambda *a, **kw: (_ for _ in ()).throw(OSError("fault")))
    with pytest.raises(OSError):
        paper_actions.advance(own, NEXT.isoformat())
    assert len(paper.load_fills(own)) == 1  # Entire SQL operation rolled back.
    monkeypatch.setattr(paper, "rebuild_positions", original)
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: paper_actions.advance(own, NEXT.isoformat()), range(4)))
    assert paper.get_account(own)["cash"] == cash_before + 1000
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: paper_actions.subscribe(own, "verified", 200), range(4)))
    assert paper.get_account(own)["cash"] == cash_before
    paper_actions.advance(own, (NEXT + timedelta(days=1)).isoformat())
    assert paper.load_positions(own)[SYM]["qty"] == 1200
    _, total, executions, _ = paper.load_fills_page(own)
    assert total == 6 and executions == 1
    assert paper.load_fills(other) == []
    assert paper_actions.status(other, NEXT.isoformat())["events"] == []
    assert_ledger(own)


def test_full_original_fees_t1_multi_account_and_rls(env, owned, monkeypatch):
    own, other = owned
    paper.create_account(own, 100000)
    paper.create_account(own, 50000, account_id="second")
    paper.create_account(other, 100000)
    order, error = paper.create_order(own, SYM, "buy", qty=200, ref_price=10, idempotency_key="buy")
    assert error is None
    assert len(paper.evaluate_intraday(own, {SYM: 10})) == 1
    assert paper.get_order(own, order["id"])["fill_price"] == 10.005
    assert paper.get_order(own, order["id"])["fees"] == 5
    assert paper.get_order(other, order["id"]) is None
    assert paper.load_fills(other) == paper.load_fills(own, "second") == []
    assert "T+1" in paper.create_order(own, SYM, "sell", qty=100)[1]
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    assert paper.overview(own)["holdings"][0]["available_qty"] == 200

    def sell(_):
        return paper.create_order(own, SYM, "sell", qty=100, ref_price=10)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(sell, range(4)))
    assert sum(error is None for _, error in responses) == 2
    assert len(paper.evaluate_intraday(own, {SYM: 10.5})) == 2
    assert paper.load_positions(own)[SYM]["qty"] == 0
    assert_ledger(own)
    with env["db"].business_transaction(own.context, write=True) as conn:
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM business.full_paper_fills")
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("UPDATE business.full_paper_fills SET value='{}'")
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute(
                """INSERT INTO business.full_paper_accounts(tenant_id,id,value)
                   VALUES(%s,'forged',%s)""", (other.context.tenant_id, '{"initial_cash":1,"cash":1,"status":"active"}'),
            )
    with env["db"].data.connection() as conn:
        assert conn.execute("SELECT * FROM business.full_paper_accounts").fetchall() == []
        assert conn.execute("SELECT * FROM business.full_paper_fills").fetchall() == []


def test_duplicate_request_and_concurrent_matching_recover_exactly_once(owned):
    own, _ = owned
    paper.create_account(own, 100000)

    def place(_):
        return paper.create_order(own, SYM, "buy", qty=100, ref_price=10, idempotency_key="request")

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(place, range(12)))
    assert len({order["id"] for order, error in responses if error is None}) == 1
    with pytest.raises(AccessError, match="参数不一致"):
        paper.create_order(own, SYM, "buy", qty=200, ref_price=10, idempotency_key="request")
    with ThreadPoolExecutor(max_workers=6) as pool:
        events = list(pool.map(lambda _: paper.evaluate_intraday(own, {SYM: 10}), range(12)))
    assert sum(len(result) for result in events) == 1
    assert len(paper.load_fills(own)) == 1
    # A separate storage object models a restarted API process.
    restart = TenantPaper(own.db, own.context, own.public_data_dir)
    assert place(0)[0]["status"] == "filled"
    assert paper.load_positions(restart)[SYM]["qty"] == 100
    assert_ledger(restart)


def test_sql_history_pages_are_bounded_while_domain_replay_stays_complete(owned, monkeypatch):
    own, _ = owned
    paper.create_account(own, 100000)
    for _ in range(7):
        order, error = paper.create_order(own, SYM, "buy", qty=100, ref_price=10)
        assert error is None and order is not None
    assert len(paper.evaluate_intraday(own, {SYM: 10})) == 7
    all_orders = sorted(
        paper.load_orders(own),
        key=lambda order: (order["created_at"], order["id"]),
        reverse=True,
    )
    all_fills = list(reversed(paper.load_fills(own)))
    assert len(all_orders) == len(all_fills) == 7
    assert paper.replay_positions(own)[0][SYM]["qty"] == 700
    for offset in range(5):
        day = (DAY + timedelta(days=offset)).isoformat()
        paper._write_nav_line(own, day, {"date": day, "cash": 1, "mv": 2, "nav": 3})

    def unbounded(*_args, **_kwargs):
        pytest.fail("HTTP history page used an unbounded SQL loader")

    monkeypatch.setattr(PaperSession, "orders", unbounded)
    monkeypatch.setattr(PaperSession, "fills", unbounded)
    monkeypatch.setattr(PaperSession, "nav", unbounded)

    orders, total, cursor = paper.load_orders_page(own, limit=2)
    assert total == 7 and [row["id"] for row in orders] == [row["id"] for row in all_orders[:2]]
    orders2, total2, _ = paper.load_orders_page(own, limit=2, before=cursor)
    assert total2 == 7 and [row["id"] for row in orders2] == [row["id"] for row in all_orders[2:4]]

    fills, fill_total, execution_total, fill_cursor = paper.load_fills_page(own, limit=3)
    assert fill_total == execution_total == 7
    assert [row["order_id"] for row in fills] == [row["order_id"] for row in all_fills[:3]]
    fills2, _, _, _ = paper.load_fills_page(own, limit=3, before=fill_cursor)
    assert [row["order_id"] for row in fills2] == [row["order_id"] for row in all_fills[3:6]]

    nav, nav_total, nav_cursor = paper.load_nav_page(own, limit=2)
    assert nav_total == 5
    assert [row["date"] for row in nav] == [
        (DAY + timedelta(days=3)).isoformat(),
        (DAY + timedelta(days=4)).isoformat(),
    ]
    older, _, _ = paper.load_nav_page(own, limit=2, before=nav_cursor)
    assert [row["date"] for row in older] == [
        (DAY + timedelta(days=1)).isoformat(),
        (DAY + timedelta(days=2)).isoformat(),
    ]


def test_fill_transaction_rollback_after_ledger_append(owned, monkeypatch):
    own, _ = owned
    paper.create_account(own, 100000)
    order, _ = paper.create_order(own, SYM, "buy", qty=100, ref_price=10)
    original = PaperSession.save_positions

    def fail(*_):
        raise RuntimeError("simulated crash after ledger and cash writes")

    monkeypatch.setattr(PaperSession, "save_positions", fail)
    with pytest.raises(RuntimeError, match="simulated crash"):
        paper.evaluate_intraday(own, {SYM: 10})
    assert paper.load_fills(own) == []
    assert paper.get_account(own)["cash"] == 100000
    assert paper.get_order(own, order["id"])["status"] == "pending"
    monkeypatch.setattr(PaperSession, "save_positions", original)
    assert len(paper.evaluate_intraday(own, {SYM: 10})) == 1
    assert_ledger(own)


def test_delayed_price_cutoff_missing_quote_retry_and_freeze(owned):
    own, _ = owned
    paper.create_account(own, 100000, queue_limit_orders=True)
    order, _ = paper.create_order(own, SYM, "buy", qty=100, order_type="next_open", ref_price=10.8)
    assert paper.settle_day(own, DAY.isoformat())["filled"] == 0
    assert paper.get_order(own, order["id"])["postponed"] == 0
    assert paper.settle_day(own, NEXT.isoformat())["filled"] == 1
    assert paper.get_order(own, order["id"])["fill_price"] == 10.5053
    stuck, _ = paper.create_order(own, "000001.SZ", "buy", qty=100, order_type="close", ref_price=10)
    for _ in range(5):
        paper.settle_day(own, NEXT.isoformat())
    assert paper.get_order(own, stuck["id"])["postponed"] == 1
    assert len(paper.load_nav(own)) == 2
    market, _ = paper.create_order(own, SYM, "buy", qty=100, ref_price=10)
    paper.set_frozen(own)
    assert paper.evaluate_intraday(own, {SYM: 10}) == []
    assert paper.get_order(own, market["id"])["status"] == "pending"
    paper.set_frozen(own, frozen=False)
    assert len(paper.evaluate_intraday(own, {SYM: 10})) == 1
    assert_ledger(own)


def test_corporate_action_fifo_stats_and_nav_replay(owned, monkeypatch):
    own, _ = owned
    paper.create_account(own, 100000, corporate_action_mode="legacy_factor")
    paper.create_order(own, SYM, "buy", qty=100, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    base = own.public_data_dir / "adj_factor"
    base.mkdir()
    pl.DataFrame({
        "symbol": [SYM], "trade_date": [NEXT], "ex_factor": [2.0],
    }).write_parquet(base / "all.parquet")
    paper.settle_day(own, NEXT.isoformat())
    paper.settle_day(own, NEXT.isoformat())
    assert paper.load_positions(own)[SYM]["qty"] == 200
    assert len([fill for fill in paper.load_fills(own) if fill["kind"] == "corp_action"]) == 1
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    sell, err = paper.create_order(own, SYM, "sell", qty=200, ref_price=10)
    assert err is None
    paper.evaluate_intraday(own, {SYM: 10.5})
    assert paper.get_order(own, sell["id"])["status"] == "filled"
    rounds = paper.round_trips(own)
    assert sum(item["qty"] for item in rounds) == 200
    assert paper.stats(own)["realized_pnl"] == pytest.approx(paper.get_account(own)["cash"] - 100000)
    assert_ledger(own)


def test_auto_trigger_atomic_zero_cooldown_and_restart_isolation(owned):
    own, other = owned
    paper.create_account(own, 100000)
    rule = paper_auto.create_auto_rule(own, {
        "name": "自动跟单", "match_kind": "rule", "match_id": "private_price", "side": "buy",
        "size_mode": "fixed_amount", "size_value": 5000, "cooldown_days": 0, "order_type": "next_open",
    })
    ev = {"id": "event-one", "source": "price", "rule_id": "private_price", "symbol": SYM, "price": 10}
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: paper_auto.on_rule_events(own, [ev]), range(12)))
    assert sum(len(result) for result in responses) == 1
    assert len(paper.load_orders(own)) == 1
    assert paper_auto.on_rule_events(TenantPaper(own.db, own.context, own.public_data_dir), [ev]) == []
    assert paper_auto.load_auto_rules(other) == []
    assert not paper_auto.delete_auto_rule(other, rule["id"])
    paper_auto.set_enabled(own, rule["id"], False)
    assert paper_auto.on_rule_events(own, [{**ev, "id": "event-two"}]) == []


def test_auto_position_slots_are_atomic_across_workers_rules_and_tenants(owned):
    own, other = owned
    config = {
        "name": "自动方向限额", "match_kind": "strategy", "match_id": "s",
        "side": "auto", "size_mode": "fixed_amount", "size_value": 1000,
        "max_positions": 1, "cooldown_days": 0, "order_type": "next_open",
    }
    for storage in (own, other):
        paper.create_account(storage, 100000)
        for _ in range(2):
            paper_auto.create_auto_rule(storage, config)
    events = [{
        "id": f"buy-{i}", "source": "strategy", "strategy_id": "s", "type": "buy_signal",
        "symbol": f"600{i:03}.SH", "price": 10,
    } for i in range(12)]

    def trigger(ev):
        restart = TenantPaper(own.db, own.context, own.public_data_dir)
        return paper_auto.on_rule_events(restart, [ev])

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(trigger, events))
    orders = paper.load_orders(own)
    assert len(orders) == 2  # Both rules can add to the same reserved symbol.
    assert len({order["symbol"] for order in orders}) == 1
    assert paper_auto.on_rule_events(own, events) == []  # All receipts survived.
    assert len(paper_auto.on_rule_events(other, [events[-1]])) == 2
    for order in orders:
        paper.cancel_order(own, order["id"])
    assert paper_auto.on_rule_events(own, events) == []  # Skipped events stay consumed.
    fresh = {**events[0], "id": "new-after-cancel", "symbol": "000001.SZ"}
    assert len(trigger(fresh)) == 2


def test_auto_full_exit_cooldown_t1_pending_and_replay_on_sql(owned, monkeypatch):
    own, other = owned
    paper.create_account(own, 100000)
    paper_auto.create_auto_rule(own, {
        "name": "自动全平", "match_kind": "strategy", "match_id": "s", "side": "auto",
        "size_mode": "pct_equity", "size_value": 1, "max_positions": 1,
        "cooldown_days": 5, "order_type": "market",
    })
    entry = {"id": "entry", "source": "strategy", "strategy_id": "s",
             "type": "pool_entry", "symbol": SYM, "price": 10}
    assert len(paper_auto.on_rule_events(own, [entry])) == 1
    paper.evaluate_intraday(own, {SYM: 10})
    # Add manually: full exit must include the entire account position.
    paper.create_order(own, SYM, "buy", qty=600, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    exit_ev = {**entry, "id": "exit-today", "type": "pool_exit"}
    assert paper_auto.on_rule_events(own, [exit_ev]) == []  # T+1 still applies.
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(NEXT, time(14), CN_TZ))
    restart = TenantPaper(own.db, own.context, own.public_data_dir)
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(
            lambda _: paper_auto.on_rule_events(restart, [], prices={SYM: 10}), range(12),
        ))
    sells = [order for result in responses for order in result]
    assert len(sells) == 1 and sells[0]["side"] == "sell" and sells[0]["qty"] == 700
    assert paper_auto.on_rule_events(restart, [exit_ev]) == []  # Receipt does not duplicate the intent.
    assert paper_auto.on_rule_events(restart, [{**exit_ev, "id": "exit-pending"}]) == []
    assert paper.load_orders(other) == []
    assert paper_auto.load_exit_intents(other) == []
    assert len(paper.evaluate_intraday(restart, {SYM: 10})) == 1
    assert paper.load_positions(restart)[SYM]["qty"] == 0
    paper_auto.retry_exit_intents(restart)
    assert paper_auto.load_exit_intents(restart)[0]["status"] == "completed"
    assert_ledger(restart)


def test_exit_intent_receipt_order_and_alert_roll_back_together(owned, monkeypatch):
    own, other = owned
    paper.create_account(own, 100000)
    paper.create_order(own, SYM, "buy", qty=700, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    paper_auto.create_auto_rule(own, {
        "name": "退出回滚", "match_kind": "strategy", "match_id": "s", "side": "auto",
        "size_mode": "fixed_amount", "size_value": 1, "cooldown_days": 5, "order_type": "market",
    })
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    original = PaperSession.put_document

    def crash(self, namespace, key, value):
        if namespace.startswith("paper_exit:") and value.get("status") == "submitted":
            raise RuntimeError("crash after exit order saved")
        return original(self, namespace, key, value)

    ev = {"id": "rollback-exit", "source": "strategy", "strategy_id": "s",
          "type": "sell_signal", "symbol": SYM, "price": 10}
    monkeypatch.setattr(PaperSession, "put_document", crash)
    with pytest.raises(RuntimeError, match="crash after exit"):
        paper_auto.on_rule_events(own, [ev])
    assert len(paper.load_orders(own)) == 1
    assert paper_auto.load_exit_intents(own) == []
    assert own.values("paper_event:default") == []
    monkeypatch.setattr(PaperSession, "put_document", original)
    assert len(paper_auto.on_rule_events(own, [ev])) == 1
    assert len(own.values("paper_event:default")) == 1
    assert paper_auto.load_exit_intents(other) == []
    assert other.values("paper_event:default") == []
    assert not (own.public_data_dir / "paper").exists()


def test_risk_snapshot_concurrent_entries_recovery_and_isolation(owned, monkeypatch):
    own, other = owned
    paper.create_account(own, 100000)
    paper.create_account(other, 100000)
    paper_auto.create_auto_rule(own, {
        "name": "政策跟单", "match_kind": "strategy", "match_id": "demo", "side": "auto",
        "size_mode": "fixed_amount", "size_value": 3000, "order_type": "market",
        "cooldown_days": 0, "risk_follow": True, "max_total_exposure_pct": 30,
    })
    event = {"id": "entry", "source": "strategy", "strategy_id": "demo",
             "type": "buy_signal", "symbol": SYM, "price": 10,
             "entry_policy": {"version": 1, "policy": {"stop_loss_pct": .06}}}
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: paper_auto.on_rule_events(own, [event]), range(8)))
    assert sum(map(len, responses)) == 1
    paper.evaluate_intraday(own, {SYM: 10})
    restart = TenantPaper(own.db, own.context, own.public_data_dir)
    paper.rebuild_positions(restart)
    assert paper.load_positions(restart)[SYM]["entry_policy"]["policy"]["stop_loss_pct"] == .06
    assert paper_auto.evaluate_position_risk(restart, {SYM: 9}) == []
    assert paper_auto.load_exit_intents(restart)[0]["risk_reason"] == "stop_loss"
    assert paper_auto.load_exit_intents(other) == []
    assert other.values("paper_risk:default") == []
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    sells = paper_auto.on_rule_events(restart, [], prices={SYM: 10})
    assert len(sells) == 1 and sells[0]["qty"] == 300
    paper.evaluate_intraday(restart, {SYM: 10})
    assert_ledger(restart)
    assert paper.load_positions(restart)[SYM]["qty"] == 0


def test_risk_peak_and_intent_rollback_together(owned, monkeypatch):
    own, _ = owned
    paper.create_account(own, 100000)
    paper_auto.create_auto_rule(own, {
        "name": "回滚政策", "match_kind": "strategy", "match_id": "demo", "side": "auto",
        "size_mode": "fixed_amount", "size_value": 3000, "order_type": "market",
        "risk_follow": True,
    })
    paper_auto.on_rule_events(own, [{
        "source": "strategy", "strategy_id": "demo", "type": "buy_signal", "symbol": SYM, "price": 10,
        "entry_policy": {"version": 1, "policy": {"stop_loss_pct": .06}},
    }])
    paper.evaluate_intraday(own, {SYM: 10})
    original = PaperSession.put_document

    def fail_intent(self, namespace, key, value):
        if namespace.startswith("paper_exit:"):
            raise RuntimeError("intent persistence failed")
        return original(self, namespace, key, value)

    with monkeypatch.context() as changes:
        changes.setattr(PaperSession, "put_document", fail_intent)
        with pytest.raises(RuntimeError, match="intent persistence failed"):
            paper_auto.evaluate_position_risk(own, {SYM: 9})
    assert own.values("paper_risk:default") == []
    assert paper_auto.load_exit_intents(own) == []
    paper_auto.evaluate_position_risk(own, {SYM: 9})
    assert len(own.values("paper_risk:default")) == 1
    assert len(paper_auto.load_exit_intents(own)) == 1


def test_sql_exit_cancel_and_expiry_recover_without_signal(owned, monkeypatch):
    own, _ = owned
    paper.create_account(own, 100000)
    paper.create_order(own, SYM, "buy", qty=700, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    rule = paper_auto.create_auto_rule(own, {
        "name": "退出重试", "match_kind": "strategy", "match_id": "s", "side": "auto",
        "size_mode": "fixed_amount", "size_value": 1, "cooldown_days": 5, "order_type": "next_open",
    })
    paper_auto.on_rule_events(own, [{
        "id": "exit", "source": "strategy", "strategy_id": "s", "type": "pool_exit",
        "symbol": SYM, "price": 10,
    }])
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    first = paper_auto.retry_exit_intents(own)[0]
    paper.cancel_order(own, first["id"])
    second = paper_auto.retry_exit_intents(own)[0]
    paper.save_order(own, {**second, "status": "expired"})
    third = paper_auto.retry_exit_intents(own)[0]
    assert len({first["id"], second["id"], third["id"]}) == 3
    paper_auto.set_enabled(own, rule["id"], False)
    assert paper.get_order(own, third["id"])["status"] == "cancelled"
    paper_auto.set_enabled(own, rule["id"], True)
    assert paper_auto.retry_exit_intents(own) == []
    assert paper_auto.load_exit_intents(own)[0]["status"] == "cancelled"


def test_sql_old_t1_exit_does_not_liquidate_same_quantity_reentry(owned, monkeypatch):
    own, _ = owned
    paper.create_account(own, 100000)
    paper.create_order(own, SYM, "buy", qty=700, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    paper_auto.create_auto_rule(own, {
        "name": "周期隔离", "match_kind": "strategy", "match_id": "s", "side": "auto",
        "size_mode": "fixed_amount", "size_value": 1, "cooldown_days": 5, "order_type": "market",
    })
    paper_auto.on_rule_events(own, [{
        "id": "old", "source": "strategy", "strategy_id": "s", "type": "sell_signal",
        "symbol": SYM, "price": 10,
    }])
    old_cycle = paper_auto.load_exit_intents(own)[0]["cycle_id"]
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT)
    paper.create_order(own, SYM, "sell", qty=700, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    paper.create_order(own, SYM, "buy", qty=700, ref_price=10)
    paper.evaluate_intraday(own, {SYM: 10})
    monkeypatch.setattr(paper, "cn_today", lambda: NEXT + timedelta(days=1))
    restart = TenantPaper(own.db, own.context, own.public_data_dir)
    assert paper_auto.on_rule_events(restart, [], prices={SYM: 10}) == []
    assert paper.load_positions(restart)[SYM]["qty"] == 700
    assert paper.load_positions(restart)[SYM]["cycle_id"] != old_cycle
    assert paper_auto.load_exit_intents(restart)[0]["status"] == "cancelled"
    assert_ledger(restart)


def test_original_http_paper_contract_alerts_and_no_shared_file_leak(env, owned):
    own, other = owned
    from app.multitenancy.alerts import Alerts

    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=own.public_data_dir),
                                     resolve_asset_type=lambda _: "stock")
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    app.include_router(router)
    # Generate fresh session cookies for the users that own these contexts.
    from test_full_identity import PASSWORD
    own_user = env["control"].execute("SELECT email FROM control.users WHERE id=%s", (own.context.user_id,)).fetchone()
    other_user = env["control"].execute("SELECT email FROM control.users WHERE id=%s", (other.context.user_id,)).fetchone()
    token, _ = env["identity"].login(own_user["email"], PASSWORD, "paper-http")
    other_token, _ = env["identity"].login(other_user["email"], PASSWORD, "paper-other-http")
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", token)
        assert client.post("/api/paper/account", json={"initial_cash": 100000}).status_code == 200
        body = {"symbol": SYM, "side": "buy", "qty": 100, "request_id": "http-order"}
        response = client.post("/api/paper/orders", json=body)
        assert response.status_code == 200, response.text
        order_id = response.json()["order"]["id"]
        assert client.post("/api/paper/orders", json=body).json()["order"]["id"] == order_id
        paper.evaluate_intraday(own, {SYM: 10})
        assert client.get("/api/paper/positions").json()["holdings"][0]["qty"] == 100
        assert client.get("/api/paper/compare").json()["accounts"][0]["account"] == "default"
        assert len(Alerts(TenantDocuments(own.db, own.context)).list_recent()) == 1
        client.cookies.set("tf_session", other_token)
        assert client.get("/api/paper/accounts").json() == {"accounts": []}
        assert client.get("/api/paper/trades").json() == {
            "fills": [], "total": 0, "execution_total": 0, "next_cursor": None,
        }
        assert client.delete("/api/paper/orders/" + order_id).status_code == 400
        assert Alerts(other).list_recent() == []
    assert not (own.public_data_dir / "paper").exists()
