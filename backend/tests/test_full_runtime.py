"""Full monitor/trading events: persistence, fencing, replay and authorization."""
import asyncio
from datetime import datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.market_time import CN_TZ, cn_today
from app.multitenancy import monitor_compute, runtime
from app.multitenancy.alerts import Alerts
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.events import EventHub, EventLog
from app.multitenancy.market import encode_frame
from app.multitenancy.monitor_rules import Rules
from app.multitenancy.notifications import Notifications
from app.multitenancy.paper_storage import TenantPaper
from app.multitenancy.runtime import FullWorker
from app.multitenancy.watchlist import Watchlist
from app.strategy import monitor_rules, paper, paper_auto


@pytest.fixture
def owned(env, tmp_path, monkeypatch):
    _, account = register(env)
    _, foreign = register(env)
    context = env["identity"].context(account, None, "worker")
    documents = TenantDocuments(env["db"], context)
    other = TenantDocuments(env["db"], env["identity"].context(foreign, None, "foreign"))
    today = cn_today()
    placed = datetime.combine(today, time(14), tzinfo=CN_TZ)
    clock = {"now": placed.timestamp() + 2}
    monkeypatch.setattr(runtime.time, "time", lambda: clock["now"])
    monkeypatch.setattr(paper, "cn_now", lambda: placed)
    monkeypatch.setattr(paper_auto, "cn_now", lambda: placed)
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: placed)
    monkeypatch.setattr(paper, "_root", lambda *_: pytest.fail("shared paper files"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    worker = FullWorker(env["db"], repo, None)
    frame = pl.DataFrame({
        "symbol": ["600519.SH", "600000.SH"], "date": [today, today],
        "close": [11., 9.], "raw_close": [11., 9.], "change_pct": [.1, .02],
        "quote_ts": [clock["now"] * 1000] * 2,
    })

    def snapshot(**changes):
        return {
            "sequence": int(clock["now"] * 1000), "captured_at": clock["now"],
            "status": {"is_trading_hours": True}, "frames": {"stock": encode_frame(frame)},
            "days": {"stock": today.isoformat()}, "indices": encode_frame(pl.DataFrame()),
            "names": {"600519.SH": "贵州茅台"}, "sectors": {}, "abnormal": [],
            **changes,
        }
    return SimpleNamespace(
        worker=worker, documents=documents, other=other, account=account,
        clock=clock, frame=frame, snapshot=snapshot, env=env, repo=repo,
    )


def price_rule(**changes):
    return monitor_rules.normalize({
        "id": "my_price", "name": "我的价格", "type": "price", "scope": "symbols",
        "symbols": ["600519.SH"], "conditions": [{"field": "close", "op": ">", "value": 10}],
        "cooldown_seconds": 3600, **changes,
    })


def test_restart_retains_cooldown_and_event_replay_is_tenant_scoped(owned):
    Rules(owned.documents).save_one(price_rule())
    tenant = owned.documents.context.tenant_id
    assert owned.worker.evaluate(tenant, owned.snapshot()) == 1
    own_events = EventLog(owned.documents).since(0)
    assert len(own_events) == 1 and own_events[0]["kind"] == "strategy_alert"
    assert EventLog(owned.other).since(0) == []
    owned.clock["now"] += 1
    restarted = FullWorker(owned.env["db"], owned.repo, None)
    assert restarted.evaluate(tenant, owned.snapshot()) == 0
    assert len(EventLog(owned.documents).since(0)) == 1
    hub = EventHub(owned.env["db"])
    own = hub.subscribe(owned.documents, "0")
    other = hub.subscribe(owned.other, "0")
    try:
        hub.poll()
        assert own.pop()[0]["payload"]["alerts"][0]["name"] == "贵州茅台"
        assert other.pop() == []
    finally:
        hub.close()


def test_rule_edit_invalidates_cooldown_and_group_snapshot_never_reads_global(owned, monkeypatch):
    from app.strategy import monitor

    monkeypatch.setattr(monitor, "_watchlist_groups_snapshot", lambda: pytest.fail("global groups"))
    watchlist = Watchlist(owned.env["db"], owned.documents.context)
    _, group = watchlist.create_group("我的分组")
    watchlist.add("600519.SH", group_id=group["id"])
    rule = price_rule(scope="watchlist_group", symbols=[], group_id=group["id"])
    Rules(owned.documents).save_one(rule)
    tenant = owned.documents.context.tenant_id
    assert owned.worker.evaluate(tenant, owned.snapshot()) == 1
    rule["conditions"] = [{"field": "close", "op": ">", "value": 10.5}]
    Rules(owned.documents).save_one(rule)
    owned.clock["now"] += 1
    assert owned.worker.evaluate(tenant, owned.snapshot()) == 1
    watchlist.remove_from_group("600519.SH", group["id"])
    owned.clock["now"] += 1
    assert owned.worker.evaluate(tenant, owned.snapshot()) == 0


def test_failed_event_commit_rolls_back_alert_and_monitor_state(owned, monkeypatch):
    import app.multitenancy.events as events

    Rules(owned.documents).save_one(price_rule())
    original = events.append_event
    monkeypatch.setattr(events, "append_event", lambda *_: (_ for _ in ()).throw(RuntimeError("event failed")))
    with pytest.raises(RuntimeError, match="event failed"):
        owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    assert Alerts(owned.documents).count() == 0
    assert owned.documents.get("monitor_runtime", "default") is None
    monkeypatch.setattr(events, "append_event", original)
    assert owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot()) == 1


def test_stale_quotes_and_expired_scheduler_owner_cannot_commit(owned):
    from app.cell.scheduler import Scheduler

    Rules(owned.documents).save_one(price_rule())
    old = owned.frame.with_columns(pl.lit((owned.clock["now"] - 60) * 1000).alias("quote_ts"))
    assert owned.worker.evaluate(
        owned.documents.context.tenant_id, owned.snapshot(frames={"stock": encode_frame(old)}),
    ) == 0
    scheduler = Scheduler(owned.env["db"])
    claims = scheduler.claim("realtime", 32)
    claim = next(row for row in claims if row["tenant_id"] == owned.documents.context.tenant_id)
    assert scheduler.finish(claim, None)
    with pytest.raises(AccessError, match="租约已过期"):
        owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot(), claim)
    assert Alerts(owned.documents).count() == 0


def test_private_monitor_uses_sandbox_and_discards_edited_input(owned):
    rule = price_rule(conditions=[{"field": "csg_private", "op": "truth"}])
    Rules(owned.documents).save_one(rule)
    calls = []

    class Executor:
        def execute(self, payload, **_kwargs):
            calls.append(payload)
            Rules(owned.documents).save_one(price_rule(name="已更新"))
            return {"events": [{"source": "signal", "symbol": "600519.SH", "message": "过期结果"}],
                    "results": {}, "state": {}}

    owned.worker.executor = Executor()
    with pytest.raises(AccessError, match="配置已更新"):
        owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    assert calls[0]["tenant_id"] == owned.documents.context.tenant_id
    assert Alerts(owned.documents).count() == 0


def test_full_worker_atomic_auto_order_and_fresh_quote_match(owned, monkeypatch):
    storage = TenantPaper(owned.env["db"], owned.documents.context, owned.repo.store.data_dir)
    paper.create_account(storage, 100000)
    monkeypatch.setattr(paper, "_prev_close", lambda *_: 11.)
    Rules(owned.documents).save_one(price_rule())
    paper_auto.create_auto_rule(storage, {
        "name": "自动买入", "match_kind": "rule", "match_id": "my_price",
        "side": "buy", "size_mode": "fixed_amount", "size_value": 11000,
        "order_type": "market", "cooldown_days": 0,
    })
    tenant = owned.documents.context.tenant_id
    owned.worker.evaluate(tenant, owned.snapshot())
    assert len(paper.load_orders(storage)) == 1
    owned.clock["now"] += 1
    # Signal state and automatic order survive a separate worker instance.
    FullWorker(owned.env["db"], owned.repo, None).evaluate(tenant, owned.snapshot())
    assert len(paper.load_orders(storage)) == 1
    assert len(paper.load_fills(storage)) == 1
    assert paper.get_account(storage)["cash"] == paper.replay_positions(storage)[1]
    assert Alerts(owned.documents).count() == 3  # monitor, automatic order, fill
    assert Alerts(owned.other).count() == 0


def test_private_strategy_batch_executes_every_security_but_notifies_once(owned):
    from test_strategy_monitor_events import _result, _SequenceStrategyEngine

    symbols = tuple(f"600{i:03}.SH" for i in range(7))
    storage = TenantPaper(owned.env["db"], owned.documents.context, owned.repo.store.data_dir)
    paper.create_account(storage, 100000)
    paper_auto.create_auto_rule(storage, {
        "name": "批量跟单", "match_kind": "strategy", "match_id": "demo", "side": "auto",
        "size_mode": "fixed_amount", "size_value": 1000, "order_type": "market", "cooldown_days": 0,
    })
    Rules(owned.documents).save_one(monitor_rules.normalize({
        "id": "batch-strategy", "name": "策略监控", "type": "strategy", "scope": "all",
        "strategy_id": "demo", "notify_events": ["pool_entry"], "cooldown_seconds": 3600,
    }))
    engine = _SequenceStrategyEngine([_result(cn_today()), _result(cn_today(), pool=symbols)])

    class Executor:
        def execute(self, payload, **_kwargs):
            return monitor_compute.evaluate_snapshot(
                payload, owned.repo, strategy_engine=engine, private_root=owned.repo.store.data_dir,
            )

    owned.worker.executor = Executor()
    frame = pl.DataFrame({
        "symbol": symbols, "close": [10.] * 7, "raw_close": [10.] * 7,
        "quote_ts": [owned.clock["now"] * 1000] * 7,
    })
    snapshot = owned.snapshot(frames={"stock": encode_frame(frame)})
    tenant = storage.context.tenant_id
    assert owned.worker.evaluate(tenant, snapshot) == 0
    owned.clock["now"] += 1
    snapshot = owned.snapshot(frames={"stock": encode_frame(frame)})
    assert owned.worker.evaluate(tenant, snapshot) == 1
    orders = paper.load_orders(storage)
    assert len(orders) == 7 and {order["symbol"] for order in orders} == set(symbols)
    assert len(Alerts(owned.documents).list_recent(source="strategy")) == 1
    assert Alerts(owned.other).count() == 0
    assert owned.worker.evaluate(tenant, snapshot) == 0
    assert len(paper.load_orders(storage)) == 7


def test_worker_retries_t1_exit_after_restart_without_monitor_events(owned, monkeypatch):
    storage = TenantPaper(owned.env["db"], owned.documents.context, owned.repo.store.data_dir)
    paper.create_account(storage, 100000)
    monkeypatch.setattr(paper, "_prev_close", lambda *_: 11.)
    paper.create_order(storage, "600519.SH", "buy", qty=700, ref_price=11.)
    paper.evaluate_intraday(storage, {"600519.SH": 11.})
    paper_auto.create_auto_rule(storage, {
        "name": "持久退出", "match_kind": "strategy", "match_id": "demo", "side": "auto",
        "size_mode": "fixed_amount", "size_value": 1, "order_type": "market", "cooldown_days": 5,
    })
    paper_auto.on_rule_events(storage, [{
        "id": "t1-exit", "source": "strategy", "strategy_id": "demo", "type": "sell_signal",
        "symbol": "600519.SH", "price": 11.,
    }])
    tomorrow = cn_today() + timedelta(days=1)
    monkeypatch.setattr(paper, "cn_today", lambda: tomorrow)
    monkeypatch.setattr(runtime, "cn_today", lambda: tomorrow)
    placed = datetime.combine(tomorrow, time(14), CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: placed)
    monkeypatch.setattr(paper_auto, "cn_now", lambda: placed)
    owned.clock["now"] = placed.timestamp() + 2
    frame = owned.frame.with_columns(pl.lit(owned.clock["now"] * 1000).alias("quote_ts"))
    snapshot = owned.snapshot(frames={"stock": encode_frame(frame)}, days={"stock": str(tomorrow)})
    restarted = FullWorker(owned.env["db"], owned.repo, None)
    tenant = storage.context.tenant_id
    assert restarted.evaluate(tenant, snapshot) == 0
    sells = [order for order in paper.load_orders(storage) if order["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == 700
    assert restarted.evaluate(tenant, snapshot) == 0
    assert paper.load_positions(storage)["600519.SH"]["qty"] == 0
    assert paper_auto.load_exit_intents(storage)[0]["status"] == "completed"


def test_slow_compute_cannot_place_entries_with_expired_prices(owned):
    storage = TenantPaper(owned.env["db"], owned.documents.context, owned.repo.store.data_dir)
    paper.create_account(storage, 100000)
    Rules(owned.documents).save_one(price_rule(conditions=[{"field": "csg_private", "op": "truth"}]))
    paper_auto.create_auto_rule(storage, {
        "name": "新鲜度", "match_kind": "rule", "match_id": "my_price", "side": "buy",
        "size_mode": "fixed_amount", "size_value": 1000, "order_type": "market", "cooldown_days": 0,
    })

    class Executor:
        def execute(self, payload, **_kwargs):
            owned.clock["now"] += 20
            return {"events": [{
                "source": "signal", "rule_id": "my_price", "symbol": "600519.SH",
                "price": 11., "message": "过期信号",
            }], "results": {}, "state": {}}

    owned.worker.executor = Executor()
    owned.worker.evaluate(storage.context.tenant_id, owned.snapshot())
    assert paper.load_orders(storage) == []


def test_notification_outbox_is_atomic_deduplicated_and_review_consumed(owned):
    sent = []

    class Transport:
        def validate_destination(self, *_):
            pass

        def send(self, channel, event, delivery_id):
            sent.append((channel["destination"], event, delivery_id))

    service = Notifications(owned.env["db"], Transport())
    channel = service.save(owned.documents, owned.account, {
        "name": "我的邮箱", "kind": "email", "destination": owned.account["email"], "enabled": True,
    })[0]
    Rules(owned.documents).save_one(price_rule(webhook_channels=[channel["id"]]))
    tenant = owned.documents.context.tenant_id
    owned.worker.evaluate(tenant, owned.snapshot())
    service.process(tenant)
    service.process(tenant)
    assert len(sent) == 1 and sent[0][0] == owned.account["email"]
    owned.documents.put("notification_preferences", "default", {"review_channels": [channel["id"]]})
    owned.documents.put("notification_requests", "private-review", {
        "user_id": owned.account["id"], "auth_version": owned.account["auth_version"],
        "state": "queued", "kind": "review", "report_id": "private-review",
        "content": "只属于本租户的复盘内容", "as_of": cn_today().isoformat(),
    })
    service.process(tenant)
    assert len(sent) == 2 and sent[-1][1]["message"] == "只属于本租户的复盘内容"
    assert owned.documents.get("notification_requests", "private-review")["state"] == "processed"
    assert owned.other.values("notification_requests") == []
    with owned.documents._transaction() as conn:
        assert conn.execute("SELECT count(*) AS n FROM business.notification_outbox WHERE status='sent'").fetchone()["n"] == 2
    assert "content" not in EventLog(owned.documents).since(0)[-1]["payload"]["alerts"][0]


def test_notification_revoked_channel_cancels_delivery_before_transport(owned):
    sent = []

    class Transport:
        def validate_destination(self, *_):
            pass

        def send(self, *_):
            sent.append(True)

    service = Notifications(owned.env["db"], Transport())
    channel = service.save(owned.documents, owned.account, {
        "name": "我的邮箱", "kind": "email", "destination": owned.account["email"], "enabled": True,
    })[0]
    Rules(owned.documents).save_one(price_rule(webhook_channels=[channel["id"]]))
    owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    service.save(owned.documents, owned.account, {**channel, "enabled": False})
    service.process(owned.documents.context.tenant_id)
    assert sent == []
    with owned.documents._transaction() as conn:
        assert conn.execute("SELECT status FROM business.notification_outbox").fetchone()["status"] == "cancelled"


def test_tenant_sse_discard_global_private_channels_and_stop_after_revoke(owned, monkeypatch):
    from app.multitenancy import events
    from app.services.quote_service import QuoteService

    hub = EventHub(owned.env["db"])
    quotes = QuoteService()
    state = SimpleNamespace(quote_service=quotes, tenant_event_hub=hub, account_identity=owned.env["identity"])
    request = SimpleNamespace(app=SimpleNamespace(state=state), state=SimpleNamespace(tenant_context=owned.documents.context),
                              headers={}, query_params={})
    request.is_disconnected = lambda: asyncio.sleep(0, result=False)
    checks = []

    def verify(_):
        checks.append(True)
        if len(checks) == 2:
            raise AccessError(401, "revoked")
        quotes.push_alerts([{"message": "other tenant private"}])
        quotes.push_review_event('{"content":"other tenant review"}')
        quotes._broadcast_quote_updated()

    monkeypatch.setattr(events, "verify_live_access", verify)

    async def collect():
        return [event async for event in events.stream(request)]

    try:
        result = asyncio.run(collect())
        assert [event["event"] for event in result] == ["quotes_updated", "access_revoked"]
        assert "other tenant" not in str(result)
        assert not quotes._subscribers and not hub.subscriptions
    finally:
        hub.close()


def test_calendar_reminder_restart_and_cross_day(owned, monkeypatch):
    from app.strategy import monitor

    today = cn_today()
    tomorrow = today + timedelta(days=1)
    Rules(owned.documents).save_one(monitor_rules.normalize({
        "id": "date_private", "name": "日期提醒", "type": "date", "scope": "symbols",
        "symbols": ["600519.SH"], "remind_date": tomorrow.isoformat(), "lead_days": 1,
    }))
    tenant = owned.documents.context.tenant_id
    assert owned.worker.evaluate(tenant, owned.snapshot()) == 1
    owned.clock["now"] += 1
    assert FullWorker(owned.env["db"], owned.repo, None).evaluate(tenant, owned.snapshot()) == 0
    monkeypatch.setattr(runtime, "cn_today", lambda: tomorrow)
    monkeypatch.setattr(monitor_compute, "cn_today", lambda: tomorrow)
    monkeypatch.setattr(monitor, "cn_today", lambda: tomorrow)
    owned.clock["now"] += 86400
    frame = owned.frame.with_columns(
        pl.lit(tomorrow).alias("date"), pl.lit(owned.clock["now"] * 1000).alias("quote_ts"),
    )
    snapshot = owned.snapshot(frames={"stock": encode_frame(frame)}, days={"stock": tomorrow.isoformat()})
    assert FullWorker(owned.env["db"], owned.repo, None).evaluate(tenant, snapshot) == 1
    assert len(EventLog(owned.documents).since(0)) == 2


def test_each_pending_order_requires_its_own_newer_quote(owned, monkeypatch):
    storage = TenantPaper(owned.env["db"], owned.documents.context, owned.repo.store.data_dir)
    paper.create_account(storage, 100000)
    monkeypatch.setattr(paper, "_prev_close", lambda *_: 11.)
    older, error = paper.create_order(storage, "600519.SH", "buy", qty=100, ref_price=11., order_type="market")
    assert error is None
    placed = datetime.combine(cn_today(), time(14, 0, 4), tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: placed)
    newer, error = paper.create_order(storage, "600519.SH", "buy", qty=100, ref_price=11., order_type="market")
    assert error is None
    owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    assert paper.get_order(storage, older["id"])["status"] == "filled"
    assert paper.get_order(storage, newer["id"])["status"] == "pending"


def test_owner_revocation_during_private_compute_discards_results(owned):
    Rules(owned.documents).save_one(price_rule(conditions=[{"field": "csg_private", "op": "truth"}]))

    class Executor:
        def execute(self, *_args, **_kwargs):
            owned.env["control"].execute(
                "UPDATE control.users SET auth_version=auth_version+1 WHERE id=%s", (owned.account["id"],),
            )
            return {"events": [{"message": "must never commit"}], "results": {}, "state": {}}

    owned.worker.executor = Executor()
    with pytest.raises(AccessError, match="权限已变更"):
        owned.worker.evaluate(owned.documents.context.tenant_id, owned.snapshot())
    assert Alerts(owned.documents).count() == 0


def test_public_quote_bridge_installs_memory_without_writing_market_files(owned, tmp_path):
    from app.multitenancy.quote_bridge import PublicQuoteBridge
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path, read_only=True)
    files_before = list(tmp_path.rglob("*"))
    repo = KlineRepository(store)
    market = SimpleNamespace(close=lambda: None)
    bridge = PublicQuoteBridge(repo, market)
    try:
        snapshot = owned.snapshot()
        market.snapshot = lambda: snapshot
        bridge.update(snapshot)
        assert repo.get_enriched_latest()[0].equals(owned.frame)
        assert bridge.status()["is_trading_hours"]
        assert list(tmp_path.rglob("*")) == files_before
        assert bridge.refresh()["is_trading_hours"]
        assert list(tmp_path.rglob("*")) == files_before
    finally:
        bridge.close()
        store.db.close()


def test_original_settlement_resumes_across_days_and_worker_restart(owned, monkeypatch):
    today, tomorrow = cn_today(), cn_today() + timedelta(days=1)
    storage = TenantPaper(owned.env["db"], owned.documents.context, owned.repo.store.data_dir)
    base = owned.repo.store.data_dir / "kline_daily"
    base.mkdir()
    pl.DataFrame({
        "symbol": ["600519.SH"] * 2, "date": [today, tomorrow],
        "open": [11., 11.2], "close": [11.1, 11.3],
    }).write_parquet(base / "bars.parquet")
    monkeypatch.setattr(paper, "_prev_close", lambda *_: 11.)
    paper.create_account(storage, 100000)
    order, error = paper.create_order(storage, "600519.SH", "buy", qty=100, ref_price=11., order_type="next_open")
    assert error is None
    snapshot = owned.snapshot(status={"is_trading_hours": False}, settlement_days=[str(today), str(tomorrow)])
    owned.worker.evaluate(storage.context.tenant_id, snapshot)
    assert paper.get_order(storage, order["id"])["status"] == "pending"
    assert owned.documents.get("paper_settlement", "default") == {"day": today.isoformat()}
    monkeypatch.setattr(runtime, "cn_today", lambda: tomorrow)
    monkeypatch.setattr(paper, "cn_today", lambda: tomorrow)
    monkeypatch.setattr(paper, "cn_now", lambda: datetime.combine(tomorrow, time(16), tzinfo=CN_TZ))
    restarted = FullWorker(owned.env["db"], owned.repo, None)
    restarted.evaluate(storage.context.tenant_id, snapshot)
    restarted.evaluate(storage.context.tenant_id, snapshot)
    assert len(paper.load_fills(storage)) == 1
    assert paper.get_order(storage, order["id"])["status"] == "filled"
    assert owned.documents.get("paper_settlement", "default") == {"day": tomorrow.isoformat()}
    assert len(paper.load_nav(storage)) == 2


def test_original_cached_api_uses_only_fresh_current_tenant_monitor_results(owned):
    from app.api.screener import _cached_with_realtime

    with owned.documents.transaction() as unit:
        unit.put("monitor_results", "default", {
            "sequence": int(owned.clock["now"] * 1000),
            "fingerprint": runtime.fingerprint(unit.connection, include_state=False),
            "results": {"private": {"total": 1, "rows": [{"symbol": "600519.SH"}], "as_of": cn_today().isoformat()}},
        })
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(account_identity=owned.env["identity"], repo=owned.repo,
                                                 monitor_engine=SimpleNamespace(latest_strategy_results=lambda: pytest.fail("global monitor")))),
        state=SimpleNamespace(tenant_context=owned.documents.context),
    )
    response = _cached_with_realtime(request)
    assert response["results"]["private"]["total"] == 1 and response["as_of"] == cn_today().isoformat()
    request.state.tenant_context = owned.other.context
    assert _cached_with_realtime(request)["results"] == {}
    request.state.tenant_context = owned.documents.context
    owned.documents.put("custom_signals", "changed", {"id": "changed"})
    assert _cached_with_realtime(request)["results"] == {}


def test_public_settlement_dates_require_final_marker_and_official_calendar(tmp_path, monkeypatch):
    import app.market_time as market_time
    from app.multitenancy.market import settlement_days

    today, tomorrow = cn_today(), cn_today() + timedelta(days=1)
    yesterday = today - timedelta(days=1)
    monkeypatch.setattr(market_time, "published_trading_days", lambda _year: {yesterday, today, tomorrow})
    for day in (yesterday, today, tomorrow):
        partition = tmp_path / "kline_daily" / f"date={day}"
        partition.mkdir(parents=True)
        (partition / "part.parquet").touch()
    assert settlement_days(tmp_path) == [str(yesterday)]
    (tmp_path / "settlement_ready.json").write_text('{"day":"' + str(today) + '"}')
    assert settlement_days(tmp_path) == [str(yesterday), str(today)]
    monkeypatch.setattr(market_time, "published_trading_days", lambda _year: {yesterday})
    assert settlement_days(tmp_path) == [str(yesterday)]


def test_index_monitor_works_when_stock_snapshot_is_unavailable(owned):
    Rules(owned.documents).save_one(price_rule(
        asset_type="index", symbols=["000001.SH"],
        conditions=[{"field": "close", "op": ">", "value": 3000}],
    ))
    frame = pl.DataFrame({
        "symbol": ["000001.SH"], "date": [cn_today()], "close": [3200.],
        "quote_ts": [owned.clock["now"] * 1000], "change_pct": [.01],
    })
    snapshot = owned.snapshot(
        frames={"stock": encode_frame(pl.DataFrame()), "index": encode_frame(frame)},
        days={"index": cn_today().isoformat()}, names={"000001.SH": "上证指数"},
    )
    assert owned.worker.evaluate(owned.documents.context.tenant_id, snapshot) == 1
    assert Alerts(owned.documents).list_recent()[0]["name"] == "上证指数"


def test_live_stream_stops_when_admin_changes_selected_tenant(owned):
    from app.multitenancy.access import verify_live_access

    identity = owned.env["identity"]
    token, admin = identity.login("admin", "Admin123", "stream-select")
    previous = admin["default_tenant_id"]
    try:
        identity.choose_tenant(admin, owned.documents.context.tenant_id, "first")
        admin = identity.current_user(token)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(account_identity=identity)),
            cookies={"tf_session": token},
            state=SimpleNamespace(account=admin, request_id="stream",
                                  tenant_context=identity.context(admin, None, "stream")),
        )
        verify_live_access(request)
        identity.choose_tenant(admin, owned.other.context.tenant_id, "second")
        with pytest.raises(AccessError, match="权限已变更"):
            verify_live_access(request)
    finally:
        owned.env["control"].execute("UPDATE control.users SET default_tenant_id=%s WHERE id=%s", (previous, admin["id"]))
