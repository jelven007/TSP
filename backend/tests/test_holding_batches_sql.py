"""Real tenant SQL transactions for named holding batches."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from test_full_monitor import app_env as app_env
from test_full_monitor import env as env
from test_full_monitor import full_cluster as full_cluster

from app.api import lots as api
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.monitor_rules import Rules
from app.strategy import lots as domain
from app.strategy import monitor_rules


def payload(key="sql_1"):
    return {"name": "SQL验证组合", "request_id": key, "stocks": [
        {"symbol": symbol, "cost_price": 10, "qty": 100, "target_pct": 6,
         "remind_date": "2026-12-01"} for symbol in ("600519.SH", "000001.SZ")]}


def test_sql_batch_atomic_retry_restart_stock_edit_and_isolation(app_env, monkeypatch):
    monkeypatch.setattr(domain, "_batch_day", lambda: "20261101")
    client = app_env["client"]
    result = client.post("/api/lots/batches", json=payload())
    assert result.status_code == 200, result.text
    batch, lots = result.json()["batch"], result.json()["lots"]
    assert batch["number"] == "2026110101"
    assert len(monitor_rules.load_all(app_env["own"])) == 4
    assert client.post("/api/lots/batches", json=payload()).json()["batch"] == batch
    assert client.post("/api/lots/batches", json={**payload(), "name": "changed"}).status_code == 409
    fresh = TenantDocuments(app_env["own"].db, app_env["own"].context)
    assert domain.load_batches(fresh) == [batch]
    assert domain.next_batch_number(fresh) == "2026110102"
    lot = lots[0]
    assert client.post("/api/lots", json={k: v for k, v in {**lot, "qty": 200}.items() if k != "batch_id"}).json()["lot"]["batch_id"] == batch["id"]
    assert client.delete("/api/lots/" + lot["id"]).status_code == 200
    assert len(domain.load_all(fresh)) == 1
    assert len(monitor_rules.load_all(fresh)) == 2
    assert domain.next_batch_number(fresh) == "2026110102"
    client.cookies.set("tf_session", app_env["other_token"])
    assert client.get("/api/lots/batches").json() == {"batches": [], "next_number": "2026110101"}
    assert client.get("/api/lots").json() == {"lots": []}


def test_sql_batch_partial_failure_rolls_back_metadata_sequence_and_all_stocks(app_env, monkeypatch):
    monkeypatch.setattr(domain, "_batch_day", lambda: "20261101")
    original = Rules.save_one
    calls = []
    def fail_third(self, value):
        calls.append(value["id"])
        if len(calls) == 3:
            raise RuntimeError("third rule failed")
        return original(self, value)
    monkeypatch.setattr(Rules, "save_one", fail_third)
    with pytest.raises(RuntimeError, match="third rule"):
        app_env["client"].post("/api/lots/batches", json=payload())
    assert domain.load_batches(app_env["own"]) == []
    assert domain.load_all(app_env["own"]) == []
    assert monitor_rules.load_all(app_env["own"]) == []
    assert domain.next_batch_number(app_env["own"]) == "2026110101"


def test_sql_concurrent_numbering_99_limit_and_daily_reset(app_env, monkeypatch):
    monkeypatch.setattr(domain, "_batch_day", lambda: "20261101")
    request = SimpleNamespace(app=app_env["app"], state=SimpleNamespace(
        tenant_context=app_env["own"].context))
    def create(n):
        return api.create_batch(api.BatchModel(**payload(f"concurrent_{n}")), request)["batch"]["number"]
    with ThreadPoolExecutor(max_workers=5) as pool:
        numbers = list(pool.map(create, range(5)))
    assert set(numbers) == {f"20261101{i:02d}" for i in range(1, 6)}
    app_env["own"].put("lot_batch_sequences", "20261101", 98)
    assert create(98) == "2026110199"
    response = app_env["client"].post("/api/lots/batches", json=payload("at_limit"))
    assert response.status_code == 409
    assert len(domain.load_batches(app_env["own"])) == 6
    monkeypatch.setattr(domain, "_batch_day", lambda: "20261102")
    assert create(100) == "2026110201"


def test_sql_delete_batch_is_tenant_scoped_cascades_and_keeps_daily_sequence(app_env, monkeypatch):
    monkeypatch.setattr(domain, "_batch_day", lambda: "20261101")
    client = app_env["client"]
    selected = client.post("/api/lots/batches", json=payload()).json()
    other = client.post("/api/lots/batches", json=payload("second")).json()
    other_request = SimpleNamespace(app=app_env["app"], state=SimpleNamespace(
        tenant_context=app_env["foreign"].context))
    foreign = api.create_batch(api.BatchModel(**payload("foreign")), other_request)
    foreign_rules = monitor_rules.load_all(app_env["foreign"])
    assert foreign["batch"]["id"] == selected["batch"]["id"]
    # A known batch number in another tenant cannot authorize deletion.
    client.cookies.set("tf_session", app_env["other_token"])
    assert client.delete("/api/lots/batches/" + other["batch"]["id"]).status_code == 404
    client.cookies.set("tf_session", app_env["token"])
    response = client.delete("/api/lots/batches/" + selected["batch"]["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "deleted_lots": 2}
    fresh = TenantDocuments(app_env["own"].db, app_env["own"].context)
    assert domain.load_batches(fresh) == [other["batch"]]
    assert {s["id"] for s in domain.load_all(fresh)} == {s["id"] for s in other["lots"]}
    assert {r["lot_id"] for r in monitor_rules.load_all(fresh)} == {s["id"] for s in other["lots"]}
    assert domain.next_batch_number(fresh) == "2026110103"
    assert domain.load_batches(app_env["foreign"]) == [foreign["batch"]]
    assert monitor_rules.load_all(app_env["foreign"]) == foreign_rules
    for lot in other["lots"]:
        assert client.delete("/api/lots/" + lot["id"]).status_code == 200
    assert client.delete("/api/lots/batches/" + other["batch"]["id"]).json()["deleted_lots"] == 0
    assert domain.next_batch_number(fresh) == "2026110103"


def test_sql_delete_batch_partial_failure_rolls_back_all_managed_definitions(app_env, monkeypatch):
    client = app_env["client"]
    selected = client.post("/api/lots/batches", json=payload()).json()
    before_lots = domain.load_all(app_env["own"])
    before_rules = monitor_rules.load_all(app_env["own"])
    original = Rules.delete_one
    calls = []
    def fail_third(self, rule_id, **kwargs):
        calls.append(rule_id)
        if len(calls) == 3:
            raise RuntimeError("delete failed")
        return original(self, rule_id, **kwargs)
    monkeypatch.setattr(Rules, "delete_one", fail_third)
    with pytest.raises(RuntimeError, match="delete failed"):
        client.delete("/api/lots/batches/" + selected["batch"]["id"])
    assert domain.load_batches(app_env["own"]) == [selected["batch"]]
    assert domain.load_all(app_env["own"]) == before_lots
    assert monitor_rules.load_all(app_env["own"]) == before_rules
