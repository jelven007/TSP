"""Saving retained holdings must not recreate explicitly retired reminder tasks."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL fixture reuse.
from test_full_identity import env, full_cluster
from test_full_monitor import app_env

from app.multitenancy.monitor_rules import Rules


def test_retained_lot_edit_and_new_lot_do_not_recreate_retired_tasks(app_env):
    client = app_env["client"]
    body = {"id": "lot_retained", "symbol": "600519.SH", "qty": 100,
            "cost_price": 10, "target_pct": 10, "remind_date": "2026-11-20"}
    assert client.post("/api/lots", json=body).status_code == 200
    documents = app_env["own"]
    assert len(Rules(documents).load_all()) == 2
    documents.put("lot_monitor_settings", "default", {"enabled": False})
    for rule in Rules(documents).load_all():
        Rules(documents).delete_one(rule["id"], managed=True)
    assert client.post("/api/lots", json={**body, "qty": 200}).status_code == 200
    assert Rules(documents).load_all() == []
    lots = client.get("/api/lots").json()["lots"]
    assert lots[0]["qty"] == 200 and lots[0]["target_pct"] == 10
    assert client.post("/api/lots", json={**body, "id": "lot_new"}).status_code == 200
    assert len(client.get("/api/lots").json()["lots"]) == 2
    assert Rules(documents).load_all() == []
    assert Rules(app_env["foreign"]).load_all() == []
