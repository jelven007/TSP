"""Tenant business integration against PostgreSQL, including actual strategy subprocess."""

# ruff: noqa: F811 -- imported pytest fixtures are injected as arguments

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

pytest.importorskip("psycopg")
from test_cell_isolation import cluster, env, headers, path  # noqa: F401

from app.cell.admin import sync_catalog
from app.cell.context import AccessError
from app.cell.identity import Identity
from app.cell.market import MarketQuote
from app.cell.paper import Paper
from app.cell.worker import CellWorker


class Market:
    def __init__(self):
        self.sequence = 100

    def quote(self, symbol="000001.SZ", price=10):
        return MarketQuote(
            symbol=symbol, price=price, change_pct=0.01, source_time=datetime.now(UTC),
            sequence=self.sequence, trading=True, suspended=False, limits_known=True,
            limit_up=11, limit_down=9,
        )

    def quotes(self, symbols):
        return {symbol: self.quote(symbol) for symbol in symbols}

    def history(self, as_of, symbols, bars):
        end = datetime.fromisoformat(as_of)
        return {"release_id": "test-shared-release", "rows": [
            {"symbol": symbol, "date": (end - timedelta(days=120 - i)).date().isoformat(),
             "open": 10 + i / 10, "high": 10.2 + i / 10, "low": 9.8 + i / 10,
             "close": 10 + i / 10, "volume": 100000 + i * 1000,
             "amount": 1e8, "is_listed": True, "is_risk_warning": False,
             "instrument_status_known": True, "listing_age_days": 1000, "price_limit_pct": 0.1}
            for symbol in symbols for i in range(121)
        ]}


def worker(env, market):
    return CellWorker(env["clients"]["a"].app.state.databases, market)


def test_strategy_catalog_config_execution_and_isolation(env):
    sync_catalog(env["control"])
    client = env["clients"]["a"]
    client.post(path(env), headers=headers(env), json={"symbols": ["000001.SZ"]})
    body = {
        "template_id": "ma_golden_cross", "name": "私有均线",
        "overrides": {"basic_filter": {"enabled": False}, "params": {
            "require_ma_golden": False, "use_volume_filter": False, "require_above_ma60": False,
        }},
    }
    saved = client.post(path(env, suffix="/strategies"), headers=headers(env), json=body)
    assert saved.status_code == 201, saved.text
    strategy = saved.json()["strategy"]
    assert client.get(path(env, "beta", "/strategies"), headers=headers(env, "other")).json()["strategies"] == []
    assert client.put(path(env, suffix="/strategies/" + strategy["id"]), headers=headers(env),
                      json={**body, "version": 99}).status_code == 409
    bad = client.post(path(env, suffix="/strategies"), headers=headers(env),
                      json={**body, "overrides": {"params": {"require_ma_golden": "bad"}}})
    assert bad.status_code == 422
    key = uuid.uuid4().hex
    run_path = path(env, suffix=f"/strategies/{strategy['id']}/run")
    payload = {"as_of": datetime.now(UTC).date().isoformat(), "idempotency_key": key}
    response = client.post(run_path, headers=headers(env), json=payload)
    assert response.status_code == 202, response.text
    assert client.post(run_path, headers=headers(env), json=payload).json()["job"]["id"] == response.json()["job"]["id"]
    assert client.post(run_path, headers=headers(env), json={**payload, "idempotency_key": "new"}).status_code == 429
    assert worker(env, Market()).process_jobs(env["tenants"]["alpha"]) == 1
    jobs = client.get(path(env, suffix="/strategy-jobs"), headers=headers(env)).json()["jobs"]
    assert jobs[0]["status"] == "succeeded", jobs
    assert jobs[0]["result"]["rows"][0]["symbol"] == "000001.SZ"
    assert jobs[0]["result"]["release_id"] == "test-shared-release"
    assert client.get(path(env, "beta", "/strategy-jobs"), headers=headers(env, "other")).json()["jobs"] == []


def test_cancel_and_membership_revocation_prevent_compute_commit(env):
    sync_catalog(env["control"])
    client = env["clients"]["a"]
    client.post(path(env), headers=headers(env), json={"symbols": ["000001.SZ"]})
    strategy = client.post(path(env, suffix="/strategies"), headers=headers(env),
                           json={"template_id": "ma_golden_cross", "name": "撤销测试"}).json()["strategy"]
    job = client.post(path(env, suffix=f"/strategies/{strategy['id']}/run"), headers=headers(env),
                      json={"as_of": "2026-10-12", "idempotency_key": "cancel"}).json()["job"]
    assert client.post(path(env, suffix=f"/strategy-jobs/{job['id']}/cancel"), headers=headers(env)).status_code == 204
    worker(env, Market()).process_jobs(env["tenants"]["alpha"])
    assert client.get(path(env, suffix="/strategy-jobs"), headers=headers(env)).json()["jobs"][0]["status"] == "cancelled"
    client.post(path(env, suffix=f"/strategies/{strategy['id']}/run"), headers=headers(env, "member"),
                json={"as_of": "2026-10-12", "idempotency_key": "revoked"})
    client.delete(path(env, suffix="/members/" + env["users"]["member"]), headers=headers(env))
    worker(env, Market()).process_jobs(env["tenants"]["alpha"])
    assert client.get(path(env, suffix="/strategy-jobs"), headers=headers(env)).json()["jobs"][0]["status"] == "failed"


def test_alert_rule_cooldown_restart_and_tenant_isolation(env):
    client = env["clients"]["a"]
    rule = {"name": "涨幅提醒", "symbols": ["000001.SZ"], "field": "change_pct", "op": ">=",
            "value": 0.005, "cooldown_seconds": 300}
    assert client.post(path(env, suffix="/monitor-rules"), headers=headers(env), json=rule).status_code == 200
    market = Market()
    assert worker(env, market).evaluate(env["tenants"]["alpha"])["alerts"] == 1
    assert worker(env, market).evaluate(env["tenants"]["alpha"])["alerts"] == 0
    market.sequence += 1
    assert worker(env, market).evaluate(env["tenants"]["alpha"])["alerts"] == 0
    inbox = client.get(path(env, suffix="/alerts"), headers=headers(env)).json()["alerts"]
    assert len(inbox) == 1 and inbox[0]["event"]["change_pct"] == 0.01
    assert client.get(path(env, "beta", "/alerts"), headers=headers(env, "other")).json()["alerts"] == []
    assert client.get(path(env, "beta", "/monitor-rules"), headers=headers(env, "other")).json()["rules"] == []
    assert client.post(path(env, suffix=f"/alerts/{inbox[0]['id']}/read"), headers=headers(env)).status_code == 204
    assert client.get(path(env, suffix="/alerts"), headers=headers(env)).json()["alerts"][0]["read_at"]


def test_paper_cash_t1_idempotency_and_cross_tenant_accounts(env):
    db = env["clients"]["a"].app.state.databases
    identity = Identity(db)
    paper = Paper(db)
    market = Market()
    with identity.authorized(env["tokens"]["owner"], env["tenants"]["alpha"], 1, "test", "paper:write") as (ctx, _):
        account = paper.create(ctx, {"name": "A账户", "initial_cash": 2000, "commission_pct": 0.00025,
                                     "stamp_tax_pct": 0.001, "slippage_bps": 5})
        body = {"symbol": "000001.SZ", "side": "buy", "qty": 100, "idempotency_key": "buy-1"}
        order = paper.order(ctx, account["id"], body, market.quote())
        assert paper.order(ctx, account["id"], body, market.quote())["id"] == order["id"]
        with pytest.raises(Exception, match="paper_insufficient_cash"):
            paper.order(ctx, account["id"], {**body, "idempotency_key": "buy-2"}, market.quote())
    market.sequence += 1
    assert worker(env, market).evaluate(env["tenants"]["alpha"])["fills"] == 1
    assert worker(env, market).evaluate(env["tenants"]["alpha"])["fills"] == 0
    with identity.authorized(env["tokens"]["owner"], env["tenants"]["alpha"], 1, "test", "paper:write") as (ctx, _):
        overview = paper.overview(ctx, account["id"])
        assert overview["account"]["cash"] == Decimal("994.50")
        assert overview["holdings"][0]["qty"] == 100 and overview["holdings"][0]["available_qty"] == 0
        with pytest.raises(Exception, match="paper_t1"):
            paper.order(ctx, account["id"], {**body, "side": "sell", "idempotency_key": "sell"}, market.quote())
    client = env["clients"]["a"]
    assert client.get(path(env, "beta", f"/paper/accounts/{account['id']}"), headers=headers(env, "other")).status_code == 404
    assert client.get(path(env, "beta", "/paper/accounts"), headers=headers(env, "other")).json()["accounts"] == []


@pytest.mark.parametrize("resource,body", [
    ("/strategies", {"template_id": "ma_golden_cross", "name": "无权策略"}),
    ("/monitor-rules", {"name": "规则", "symbols": ["000001.SZ"], "field": "price", "op": ">", "value": 10}),
    ("/paper/accounts", {"name": "无权账户", "initial_cash": 10000}),
])
def test_viewer_cannot_write_business_modules(env, resource, body):
    assert env["clients"]["a"].post(path(env, suffix=resource), headers=headers(env, "viewer"), json=body).status_code == 403


@pytest.mark.parametrize("during_compute", [False, True])
def test_token_revocation_prevents_strategy_execution_and_result_commit(env, during_compute):
    sync_catalog(env["control"])
    client = env["clients"]["a"]
    client.post(path(env), headers=headers(env), json={"symbols": ["000001.SZ"]})
    strategy = client.post(path(env, suffix="/strategies"), headers=headers(env),
                           json={"template_id": "ma_golden_cross", "name": "Token复核"}).json()["strategy"]
    token = client.post(path(env, suffix="/tokens"), headers=headers(env),
                        json={"name": "worker", "permissions": ["strategies:write"]}).json()
    client.post(path(env, suffix=f"/strategies/{strategy['id']}/run"), headers=headers(env, token=token["token"]),
                json={"as_of": "2026-10-12", "idempotency_key": "token"})
    calls = []

    def compute(payload, timeout):
        calls.append(1)
        assert client.delete(path(env, suffix="/tokens/" + token["id"]), headers=headers(env)).status_code == 204
        return {"total": 1, "rows": [{"symbol": "000001.SZ"}]}

    if not during_compute:
        client.delete(path(env, suffix="/tokens/" + token["id"]), headers=headers(env))
    runner = CellWorker(client.app.state.databases, Market(), compute)
    runner.process_jobs(env["tenants"]["alpha"])
    job = client.get(path(env, suffix="/strategy-jobs"), headers=headers(env)).json()["jobs"][0]
    assert job["status"] == "failed" and job["error"] == "job_authorization_changed" and job["result"] is None
    assert len(calls) == int(during_compute)


def test_paper_concurrent_match_cancel_sell_and_append_only_ledger(env):
    from psycopg.errors import InsufficientPrivilege

    client = env["clients"]["a"]
    db, market = client.app.state.databases, Market()
    identity, paper = Identity(db), Paper(db)

    def access():
        return identity.authorized(env["tokens"]["owner"], env["tenants"]["alpha"], 1, "test", "paper:write")

    with access() as (ctx, _):
        account = paper.create(ctx, {"name": "并发", "initial_cash": 10000, "commission_pct": .00025,
                                     "stamp_tax_pct": .001, "slippage_bps": 5})
        body = {"symbol": "000001.SZ", "side": "buy", "qty": 100, "idempotency_key": "race"}
        first = paper.order(ctx, account["id"], body, market.quote())
    market.sequence += 1

    def match(_):
        return worker(env, market).evaluate(env["tenants"]["alpha"])["fills"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(match, range(4))) == 1
    # Advance the prior fill's date only through the migration/admin role to exercise T+1 sells.
    env["data"]["a"].execute(
        "UPDATE business.paper_fills SET trade_date=trade_date-1 WHERE tenant_id=%s AND account_id=%s",
        (env["tenants"]["alpha"], account["id"]),
    )

    def sell(index):
        try:
            with access() as (ctx, _):
                return paper.order(ctx, account["id"], {**body, "side": "sell", "idempotency_key": f"sell-{index}"}, market.quote())
        except AccessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(sell, range(2)))
    assert sum(isinstance(response, dict) for response in responses) == 1
    assert "paper_t1_or_insufficient_position" in responses
    queued = next(response for response in responses if isinstance(response, dict))
    market.sequence += 1

    def cancel():
        try:
            with access() as (ctx, _):
                paper.cancel(ctx, account["id"], queued["id"])
        except AccessError as exc:
            assert exc.code == "paper_order_not_cancellable"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(match, 0), executor.submit(cancel)]
        for future in futures:
            future.result()
    with access() as (ctx, _):
        overview = paper.overview(ctx, account["id"])
        assert len(overview["fills"]) in (1, 2)
        assert next(order for order in overview["orders"] if order["id"] == queued["id"])["status"] in {"filled", "cancelled"}
        assert paper.order(ctx, account["id"], body, market.quote().model_copy(
            update={"source_time": datetime.now(UTC) - timedelta(seconds=60)}
        ))["id"] == first["id"]
        with db.business_transaction(ctx, permission="paper:write") as conn:
            for statement in ("UPDATE business.paper_fills SET qty=1", "DELETE FROM business.paper_fills"):
                with pytest.raises(InsufficientPrivilege), conn.transaction():
                    conn.execute(statement)


def test_paper_bands_stale_freeze_and_day_expiry(env):
    client = env["clients"]["a"]
    db, market = client.app.state.databases, Market()
    identity, paper = Identity(db), Paper(db)
    with identity.authorized(env["tokens"]["owner"], env["tenants"]["alpha"], 1, "test", "paper:write") as (ctx, _):
        account = paper.create(ctx, {"name": "边界", "initial_cash": 10000, "commission_pct": .00025,
                                     "stamp_tax_pct": .001, "slippage_bps": 5})
        body = {"symbol": "000001.SZ", "side": "buy", "qty": 100, "idempotency_key": "bands"}
        with pytest.raises(AccessError, match="quote_not_tradable"):
            paper.order(ctx, account["id"], body, market.quote().model_copy(update={"limits_known": False}))
        order = paper.order(ctx, account["id"], body, market.quote())
        paper.freeze(ctx, account["id"], True)
    market.sequence += 1
    assert worker(env, market).evaluate(env["tenants"]["alpha"])["fills"] == 0
    with identity.authorized(env["tokens"]["owner"], env["tenants"]["alpha"], 1, "test", "paper:write") as (ctx, _):
        paper.freeze(ctx, account["id"], False)
        with db.business_transaction(ctx, write=True, permission="paper:write") as conn:
            quote = market.quote().model_copy(update={"price": 11.})
            assert Paper.match(conn, ctx, {quote.symbol: quote}) == 0
    env["data"]["a"].execute(
        "UPDATE business.paper_orders SET created_at=created_at-interval '1 day' WHERE tenant_id=%s AND id=%s",
        (env["tenants"]["alpha"], order["id"]),
    )
    assert worker(env, market).evaluate(env["tenants"]["alpha"])["fills"] == 0
    overview = client.get(path(env, suffix=f"/paper/accounts/{account['id']}"), headers=headers(env)).json()
    assert overview["orders"][0]["status"] == "expired" and overview["orders"][0]["reason"] == "paper_day_expired"


@pytest.mark.parametrize("change", ["cancel", "lease"])
def test_strategy_cancel_and_expired_attempt_cannot_commit_result(env, change):
    sync_catalog(env["control"])
    client = env["clients"]["a"]
    client.post(path(env), headers=headers(env), json={"symbols": ["000001.SZ"]})
    strategy = client.post(path(env, suffix="/strategies"), headers=headers(env),
                           json={"template_id": "ma_golden_cross", "name": "执行防护"}).json()["strategy"]
    job = client.post(path(env, suffix=f"/strategies/{strategy['id']}/run"), headers=headers(env),
                      json={"as_of": "2026-10-12", "idempotency_key": change}).json()["job"]

    def compute(payload, timeout):
        if change == "cancel":
            client.post(path(env, suffix=f"/strategy-jobs/{job['id']}/cancel"), headers=headers(env))
        else:
            env["data"]["a"].execute(
                "UPDATE business.strategy_jobs SET lease_until=now()-interval '1 second' WHERE tenant_id=%s AND id=%s",
                (env["tenants"]["alpha"], job["id"]),
            )
        return {"total": 1, "rows": [{"symbol": "000001.SZ"}]}

    runner = CellWorker(client.app.state.databases, Market(), compute)
    runner.process_jobs(env["tenants"]["alpha"])
    runner.process_jobs(env["tenants"]["alpha"])
    saved = client.get(path(env, suffix="/strategy-jobs"), headers=headers(env)).json()["jobs"][0]
    assert saved["status"] == ("cancelled" if change == "cancel" else "interrupted")
    assert saved["result"] is None
    assert client.get(path(env, suffix="/alerts"), headers=headers(env)).json()["alerts"] == []
