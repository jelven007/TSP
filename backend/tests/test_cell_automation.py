"""Paper-only automation: signal timing, account budgets, revocation and risk exits."""

# ruff: noqa: F811
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("psycopg")
from test_cell_business import Market
from test_cell_isolation import cluster, env, headers, path  # noqa: F401

from app.cell.admin import sync_catalog
from app.cell.automation import Automations
from app.cell.identity import Identity
from app.cell.paper import Paper
from app.cell.worker import CellWorker


def setup_automation(env, user="owner"):
    sync_catalog(env["control"])
    client = env["clients"]["a"]
    client.post(path(env), headers=headers(env), json={"symbols": ["000001.SZ", "600000.SH"]})
    strategy = client.post(path(env, suffix="/strategies"), headers=headers(env),
                           json={"template_id": "ma_golden_cross", "name": "自动选股"}).json()["strategy"]
    account = client.post(path(env, suffix="/paper/accounts"), headers=headers(env),
                          json={"name": "自动账户", "initial_cash": 10000}).json()["account"]
    body = {"name": "自动交易", "strategy_id": strategy["id"], "account_id": account["id"],
            "enabled": True, "run_time": "09:35", "max_positions": 1, "budget_per_order": 2000,
            "stop_loss_pct": .05, "take_profit_pct": .1, "max_daily_orders": 4}
    response = client.post(path(env, suffix="/trade-automations"), headers=headers(env, user), json=body)
    assert response.status_code == 200, response.text
    return client.app.state.databases, response.json()["automation"], account, strategy, body


def run_signal(env, db, automation, strategy, market):
    # Queue through the production daily scheduler. `now` only controls the schedule clock.
    clock = datetime.now(UTC).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai")).replace(hour=10, minute=0)
    assert Automations(db, market).process(env["tenants"]["alpha"], now=clock) == 0
    captured = []
    def compute(payload, timeout):
        captured.append(payload)
        return {"total": 2, "rows": [{"symbol": "000001.SZ"}, {"symbol": "600000.SH"}]}
    assert CellWorker(db, market, compute).process_jobs(env["tenants"]["alpha"]) == 1
    assert captured[0]["as_of"] < clock.date().isoformat()
    assert captured[0]["strategy"]["id"] == strategy["id"]
    return clock


def test_daily_signal_orders_later_quote_only_budget_and_isolation(env):
    db, automation, account, strategy, body = setup_automation(env)
    market = Market()
    clock = run_signal(env, db, automation, strategy, market)
    tenant = env["tenants"]["alpha"]
    # Same day reruns cannot queue a second daily job.
    Automations(db, market).process(tenant, now=clock)
    market.sequence += 1
    assert CellWorker(db, market).evaluate(tenant)["fills"] == 1
    assert CellWorker(db, market).evaluate(tenant)["fills"] == 0
    client = env["clients"]["a"]
    overview = client.get(path(env, suffix=f"/paper/accounts/{account['id']}"), headers=headers(env)).json()
    assert len(overview["orders"]) == len(overview["fills"]) == 1
    assert overview["orders"][0]["qty"] == 100
    assert overview["orders"][0]["automation_id"] == automation["id"]
    assert overview["fills"][0]["quote_sequence"] > overview["orders"][0]["quote_sequence"]
    assert overview["holdings"][0]["available_qty"] == 0
    assert len(client.get(path(env, suffix="/strategy-jobs"), headers=headers(env)).json()["jobs"]) == 1
    assert client.get(path(env, "beta", "/trade-automations"), headers=headers(env, "other")).json()["automations"] == []
    assert client.get(path(env, "beta", "/trade-intents"), headers=headers(env, "other")).json()["intents"] == []
    assert client.post(path(env, suffix="/trade-automations"), headers=headers(env, "viewer"), json=body).status_code == 403


def test_signal_time_future_quote_and_pause_cancel_pending(env):
    db, automation, _account, strategy, body = setup_automation(env)
    market = Market()
    clock = run_signal(env, db, automation, strategy, market)
    tenant = env["tenants"]["alpha"]
    data = env["data"]["a"]
    data.execute("UPDATE business.trade_intents SET signal_at=now()+interval '10 seconds' WHERE tenant_id=%s", (tenant,))
    Automations(db, market).process(tenant, now=clock)
    assert not data.execute("SELECT 1 FROM business.paper_orders WHERE tenant_id=%s", (tenant,)).fetchone()
    data.execute("UPDATE business.trade_intents SET signal_at=now()-interval '1 second' WHERE tenant_id=%s", (tenant,))
    Automations(db, market).process(tenant, now=clock)
    saved = env["clients"]["a"].post(path(env, suffix="/trade-automations"), headers=headers(env), json={
        **body, "id": automation["id"], "version": automation["version"], "enabled": False,
    })
    assert saved.status_code == 200
    market.sequence += 1
    assert CellWorker(db, market).evaluate(tenant)["fills"] == 0
    assert data.execute("SELECT status FROM business.paper_orders WHERE tenant_id=%s", (tenant,)).fetchone()["status"] == "cancelled"


def test_revocation_cancels_auto_orders_before_matching(env):
    db, automation, _account, strategy, _body = setup_automation(env, "member")
    market = Market()
    clock = run_signal(env, db, automation, strategy, market)
    Automations(db, market).process(env["tenants"]["alpha"], now=clock)
    client = env["clients"]["a"]
    client.delete(path(env, suffix="/members/" + env["users"]["member"]), headers=headers(env))
    market.sequence += 1
    assert CellWorker(db, market).evaluate(env["tenants"]["alpha"])["fills"] == 0
    rows = client.get(path(env, suffix="/trade-automations"), headers=headers(env)).json()["automations"]
    assert not rows[0]["enabled"] and rows[0]["error"] == "job_authorization_changed"


def test_risk_exit_t1_and_concurrent_intent_consumption(env):
    db, automation, account, strategy, _body = setup_automation(env)
    market = Market()
    clock = run_signal(env, db, automation, strategy, market)
    Automations(db, market).process(env["tenants"]["alpha"], now=clock)
    market.sequence += 1
    CellWorker(db, market).evaluate(env["tenants"]["alpha"])
    class DownMarket(Market):
        def quote(self, symbol="000001.SZ", price=10):
            return super().quote(symbol, 9.4)
    down = DownMarket()
    down.sequence = 200
    tenant = env["tenants"]["alpha"]
    assert CellWorker(db, down).evaluate(tenant)["fills"] == 0
    assert not env["data"]["a"].execute(
        "SELECT 1 FROM business.trade_intents WHERE tenant_id=%s AND side='sell'", (tenant,)
    ).fetchone()
    env["data"]["a"].execute("UPDATE business.paper_fills SET trade_date=trade_date-1 WHERE tenant_id=%s", (tenant,))
    CellWorker(db, down).evaluate(tenant)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: Automations(db, down).process(tenant, now=clock), range(2)))
    down.sequence += 1
    assert CellWorker(db, down).evaluate(tenant)["fills"] == 1
    assert env["data"]["a"].execute(
        "SELECT count(*) AS n FROM business.paper_orders WHERE tenant_id=%s AND side='sell'", (tenant,)
    ).fetchone()["n"] == 1
    with Identity(db).worker_context(tenant) as (ctx, _), db.business_transaction(ctx, permission="paper:write") as conn:
        assert Paper._account(conn, account["id"])["cash"] > 9000


def test_expired_signal_and_price_gap_cannot_exceed_budget(env):
    db, automation, account, strategy, _body = setup_automation(env)
    market = Market()
    clock = run_signal(env, db, automation, strategy, market)
    tenant = env["tenants"]["alpha"]
    # Strategy intent expiry must produce a visible skip, without creating an order.
    env["data"]["a"].execute(
        "UPDATE business.trade_intents SET expires_at=%s WHERE tenant_id=%s",
        (clock - timedelta(seconds=1), tenant),
    )
    Automations(db, market).process(tenant, now=clock)
    assert env["data"]["a"].execute(
        "SELECT status,reason FROM business.trade_intents WHERE tenant_id=%s", (tenant,)
    ).fetchone() == {"status": "skipped", "reason": "automation_signal_expired"}
    # Submit with a tight budget then gap upwards before match, while remaining inside the band.
    env["data"]["a"].execute(
        "UPDATE business.trade_automations SET budget_per_order=1050 WHERE tenant_id=%s", (tenant,)
    )
    with Identity(db).worker_context(tenant) as (ctx, _), db.business_transaction(ctx, write=True, permission="paper:write") as conn:
        row = conn.execute("SELECT * FROM business.trade_automations WHERE id=%s", (automation["id"],)).fetchone()
        Paper.submit(conn, ctx, account["id"], {
            "symbol": "000001.SZ", "side": "buy", "qty": 100, "idempotency_key": "gap",
        }, market.quote(), row)
    market.sequence += 1
    quote = market.quote(price=10.8)
    assert CellWorker(db, market).evaluate(tenant, {quote.symbol: quote})["fills"] == 0
    row = env["data"]["a"].execute(
        "SELECT status,reason,reserved_cash FROM business.paper_orders WHERE tenant_id=%s", (tenant,)
    ).fetchone()
    assert row["status"] == "expired" and row["reason"] == "automation_budget_exceeded" and row["reserved_cash"] == 0
