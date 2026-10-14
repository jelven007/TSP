"""Full original monitor and lot persistence: scoped API and atomic related state."""
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.api import alerts, lots, monitor_rules, watchlist
from app.cell.context import AccessError
from app.multitenancy import access
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.monitor_rules import Rules
from app.services import alert_store
from app.strategy import lots as lot_domain
from app.strategy import monitor_rules as rule_domain


def rule(**changes):
    return rule_domain.normalize({
        "id": "private_price", "name": "价格提醒", "type": "price", "scope": "symbols",
        "symbols": ["600519.SH"], "conditions": [{"field": "close", "op": ">", "value": 10}],
        **changes,
    })


@pytest.fixture
def app_env(env, tmp_path, monkeypatch):
    first_token, first = register(env)
    other_token, other = register(env)
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), resolve_asset_type=lambda _: "stock")
    app.state.monitor_engine = SimpleNamespace(set_rules=lambda _: pytest.fail("shared monitor mutation"))
    app.state.quote_service = SimpleNamespace(push_alerts=lambda _: pytest.fail("shared alert broadcast"))
    app.state.capabilities = SimpleNamespace(has=lambda _: False)
    app.add_exception_handler(AccessError, access.access_error)
    app.middleware("http")(access.middleware)
    for module in (alerts, lots, monitor_rules, watchlist):
        app.include_router(module.router)
    monkeypatch.setattr(rule_domain, "_dir", lambda *_: pytest.fail("global rule files"))
    monkeypatch.setattr(lot_domain, "_dir", lambda *_: pytest.fail("global lot files"))
    monkeypatch.setattr(alert_store, "_path", lambda *_: pytest.fail("global alert history"))
    own = TenantDocuments(env["db"], env["identity"].context(first, None, "monitor"))
    foreign = TenantDocuments(env["db"], env["identity"].context(other, None, "monitor-other"))
    with TestClient(app, base_url="https://testserver", headers={"Origin": "https://testserver"}) as client:
        client.cookies.set("tf_session", first_token)
        yield {"client": client, "own": own, "foreign": foreign, "token": first_token,
               "other_token": other_token, "app": app}


def test_original_monitor_rule_http_and_group_ownership(app_env):
    client = app_env["client"]
    response = client.post("/api/monitor-rules", json=rule())
    assert response.status_code == 200, response.text
    assert client.get("/api/monitor-rules").json()["rules"][0]["name"] == "价格提醒"
    group = client.post("/api/watchlist/groups", json={"name": "我的", "color": "sky"}).json()["group"]
    grouped = rule(id="grouped", scope="watchlist_group", symbols=[], group_id=group["id"])
    assert client.post("/api/monitor-rules", json=grouped).status_code == 200
    client.cookies.set("tf_session", app_env["other_token"])
    assert client.get("/api/monitor-rules").json() == {"rules": []}
    assert client.post("/api/monitor-rules", json=grouped).status_code == 400
    assert client.delete("/api/monitor-rules/private_price").status_code == 404
    assert rule_domain.load_all(app_env["foreign"]) == []
    client.cookies.set("tf_session", app_env["token"])
    client.delete("/api/watchlist/groups/" + group["id"])
    assert "runtime_warning" in next(row for row in client.get("/api/monitor-rules").json()["rules"] if row["id"] == "grouped")


def test_original_lot_api_atomic_rules_hosting_and_edit(app_env):
    client = app_env["client"]
    body = {"id": "lot_private", "symbol": "600519.SH", "qty": 100, "cost_price": 10,
            "target_pct": 10, "remind_date": "2026-10-20", "lead_days": 1}
    response = client.post("/api/lots", json=body)
    assert response.status_code == 200, response.text
    saved = client.get("/api/monitor-rules").json()["rules"]
    assert len(saved) == 2 and all(row["lot_id"] == body["id"] for row in saved)
    assert client.post("/api/monitor-rules", json=rule(id="lot_private_p")).status_code == 409
    assert client.delete("/api/monitor-rules/lot_private_p").status_code == 409
    assert client.post("/api/lots", json={**body, "remind_date": None}).status_code == 200
    assert len(client.get("/api/monitor-rules").json()["rules"]) == 1
    client.cookies.set("tf_session", app_env["other_token"])
    assert client.get("/api/lots").json() == {"lots": []}
    assert client.delete("/api/lots/lot_private").status_code == 200
    assert len(lot_domain.load_all(app_env["own"])) == 1
    client.cookies.set("tf_session", app_env["token"])
    assert client.delete("/api/lots/lot_private").status_code == 200
    assert client.get("/api/lots").json() == {"lots": []}
    assert client.get("/api/monitor-rules").json() == {"rules": []}


def test_lot_partial_write_failure_rolls_back_all_related_records(app_env, monkeypatch):
    original = Rules.save_one
    attempts = []

    def fail_after_first(self, value):
        attempts.append(value["id"])
        if len(attempts) == 2:
            raise RuntimeError("failed second derived rule")
        return original(self, value)

    monkeypatch.setattr(Rules, "save_one", fail_after_first)
    with pytest.raises(RuntimeError, match="failed second"):
        app_env["client"].post("/api/lots", json={
            "id": "lot_rollback", "symbol": "600519.SH", "cost_price": 10,
            "target_pct": 10, "remind_date": "2026-10-20",
        })
    assert lot_domain.load_all(app_env["own"]) == []
    assert rule_domain.load_all(app_env["own"]) == []


def test_related_document_transaction_rollback_and_no_read_upgrade(app_env):
    documents = app_env["own"]
    with pytest.raises(RuntimeError, match="undo"), documents.transaction() as unit:
        unit.put("related", "one", {"value": 1})
        unit.put("related", "two", {"value": 2})
        raise RuntimeError("undo")
    assert documents.values("related") == []
    with documents.transaction(write=False) as unit, pytest.raises(RuntimeError, match="read transaction"):
        unit.put("related", "one", {})


def test_sql_alert_history_never_publishes_private_payload_to_global_bus(app_env, monkeypatch):
    from app.services.events import bus
    monkeypatch.setattr(bus, "publish", lambda *_a, **_k: pytest.fail("global event bus"))
    own = app_env["own"]
    now = int(time.time() * 1000)
    events = [
        {"id": "stable-alert", "ts": now, "source": "price", "type": "price", "symbol": "600519.SH", "message": "我的提醒"},
        {"id": "second-alert", "ts": now + 1, "source": "paper", "type": "fill", "symbol": "600519.SH", "message": "成交"},
    ]
    alert_store.append_many(own, events)
    alert_store.append_many(own, events)
    assert alert_store.count(own) == 2 and alert_store.count(app_env["foreign"]) == 0
    assert alert_store.list_recent(own, source="paper")[0]["message"] == "成交"
    client = app_env["client"]
    assert client.get("/api/alerts").json()["total"] == 2
    client.cookies.set("tf_session", app_env["other_token"])
    assert client.get("/api/alerts").json() == {"alerts": [], "total": 0}
    assert client.delete("/api/alerts/" + str(now)).status_code == 404
    client.cookies.set("tf_session", app_env["token"])
    assert client.delete("/api/alerts/" + str(now)).status_code == 200
    assert client.delete("/api/alerts").json()["cleared"] == 1
