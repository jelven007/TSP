"""Lifecycle integration with real PostgreSQL, runtime permissions and RLS."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from psycopg.errors import InsufficientPrivilege
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_paper import DAY, SYM
from test_full_paper import owned as owned

from app.cell.admin import migrate
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.paper_storage import PaperSession
from app.strategy import paper


@pytest.fixture
def schema8(env):
    for kind, role in zip(("control", "data"), env["roles"], strict=True):
        migrate(env[kind], kind, role, "full", target_version=8)
        migrate(env[kind], kind, role, "full", target_version=8)
    env["db"].settings = env["db"].settings.model_copy(update={"schema_version": 8})
    env["db"].check()


def test_sql_account_edit_and_transactional_cascade(env, owned, schema8, monkeypatch):
    own, other = owned
    for storage, aid in ((own, "default"), (own, "keep"), (other, "default")):
        paper.create_account(storage, 200_000, account_id=aid)
        paper.create_order(storage, SYM, "buy", qty=100, ref_price=10, account_id=aid)
        paper.evaluate_intraday(storage, {SYM: 10}, aid)
        paper._write_nav_line(storage, DAY.isoformat(), {"date": DAY.isoformat(), "nav": 200_000}, aid)
        docs = TenantDocuments(storage.db, storage.context)
        for kind in ("auto", "event", "exit", "exit_archive", "risk", "auto_skips"):
            docs.put(f"paper_{kind}:{aid}", "record", {"kept": True})
        docs.put("strategy_sources", "shared", {"name": "共享策略"})
    before = paper.load_fills(own)
    for tenant, path in (
        (own.context.tenant_id, "paper/accounts/default/account.json"),
        (own.context.tenant_id, "paper/accounts/keep/account.json"),
        (own.context.tenant_id, "strategies/shared.py"),
        (other.context.tenant_id, "paper/accounts/default/account.json"),
    ):
        env["data"].execute(
            "INSERT INTO business.legacy_artifacts(tenant_id,path,content,sha256) VALUES(%s,%s,%s,%s)",
            (tenant, path, b"fixture", "0" * 64),
        )
    paper.update_account(own, name="新名", commission_pct=.001)
    assert paper.overview(own)["account_name"] == "新名"
    assert paper.load_fills(own) == before
    with pytest.raises(ValueError, match="初始资金"):
        paper.update_account(own, initial_cash=500_000)
    with own.db.business_transaction(own.context, write=True) as conn:
        conn.execute(
            """INSERT INTO business.notification_channels
               (tenant_id,id,name,kind,destination,user_id,policy_version,routing_epoch)
               VALUES(%s,'channel','测试','email','fixture@example.test',%s,1,1)""",
            (own.context.tenant_id, own.context.user_id),
        )
        alert_id = conn.execute("SELECT id FROM business.alerts WHERE event->>'account_id'='default'").fetchone()["id"]
        conn.execute(
            """INSERT INTO business.notification_outbox(tenant_id,id,channel_id,channel_version,alert_id)
               VALUES(%s,'delivery','channel',1,%s)""", (own.context.tenant_id, alert_id),
        )
        # Mixed event batch must retain alerts belonging to another account.
        conn.execute(
            """INSERT INTO business.full_events(tenant_id,kind,payload) VALUES(%s,'strategy_alert',%s)""",
            (own.context.tenant_id, '{"alerts":[{"source":"paper","account_id":"default"},{"source":"paper","account_id":"keep"}]}'),
        )
    original = PaperSession.delete_account

    def fail(self, account_id):
        original(self, account_id)
        raise OSError("fixture after cascade")
    monkeypatch.setattr(PaperSession, "delete_account", fail)
    with pytest.raises(OSError):
        paper.delete_account(own)
    assert paper.load_fills(own) == before and paper.get_account(own) is not None
    monkeypatch.setattr(PaperSession, "delete_account", original)
    assert paper.delete_account(own) is True
    assert paper.get_account(own) is None
    assert paper.load_orders(own) == paper.load_fills(own) == paper.load_nav(own) == []
    assert len(paper.load_fills(own, "keep")) == len(paper.load_fills(other)) == 1
    assert TenantDocuments(own.db, own.context).get("strategy_sources", "shared") == {"name": "共享策略"}
    from app.multitenancy.alerts import append_in_transaction

    with own.db.business_transaction(own.context, write=True) as conn:
        assert append_in_transaction(conn, own.context.tenant_id, [
            {"id": "late", "source": "paper", "account_id": "default", "ts": 1},
        ]) == []
    with own.db.business_transaction(own.context) as conn:
        assert conn.execute("SELECT * FROM business.documents WHERE namespace=ANY(%s)", (
            [f"paper_{kind}:default" for kind in ("auto", "event", "exit", "exit_archive", "risk", "auto_skips")],
        )).fetchall() == []
        assert conn.execute("SELECT * FROM business.notification_outbox").fetchall() == []
        assert {row["path"] for row in conn.execute("SELECT path FROM business.legacy_artifacts").fetchall()} == {
            "paper/accounts/keep/account.json", "strategies/shared.py",
        }
        assert conn.execute("SELECT * FROM business.alerts WHERE event->>'account_id'='default'").fetchall() == []
        events = conn.execute("SELECT payload FROM business.full_events WHERE kind='strategy_alert'").fetchall()
        assert any(row["payload"]["alerts"] == [{"source": "paper", "account_id": "keep"}] for row in events)
        assert not any(e.get("account_id") == "default" for row in events for e in row["payload"]["alerts"])
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM business.full_paper_fills")


def test_sql_unused_capital_and_delete_matching_concurrency(owned, schema8):
    own, other = owned
    paper.create_account(own, 200_000)
    paper.create_account(other, 200_000)
    assert paper.update_account(own, initial_cash=500_000)["cash"] == 500_000
    paper.create_order(own, SYM, "buy", qty=100, ref_price=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda op: op(), [
            lambda: paper.evaluate_intraday(own, {SYM: 10}),
            lambda: paper.delete_account(own),
        ]))
    assert paper.get_account(own) is None
    assert paper.load_fills(own) == []
    assert paper.get_account(other)["cash"] == 200_000
