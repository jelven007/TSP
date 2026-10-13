"""Capacity-based packing and real SQL admission races."""

# ruff: noqa: F811
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("psycopg")
from test_cell_isolation import cluster, env, headers, path  # noqa: F401

from app.cell.admin import provision_tenant
from app.cell.placement import CellLoad, planning_loads, select_cell, split_plan


def test_weighted_packing_heavy_tenants_and_fixed_capacity():
    cells = [CellLoad("a", "active", 0, 1000, 0, 200), CellLoad("b", "active", 0, 1000, 0, 200)]
    tenants = [{"tenant_id": f"heavy-{i}", "realtime_units": 20} for i in range(10)]
    tenants += [{"tenant_id": f"light-{i}", "realtime_units": 1} for i in range(200)]
    plan = split_plan(tenants, cells)
    assert len(plan["assignments"]) == 210
    assert all(c["realtime_units"] == 200 for c in plan["cells"])
    with pytest.raises(ValueError, match="no Cell"):
        split_plan([*tenants, {"tenant_id": "over", "realtime_units": 1}], cells)
    assert select_cell([CellLoad("full", "active", 10, 1000, 195, 200),
                        CellLoad("free", "active", 50, 1000, 0, 200),
                        CellLoad("draining", "draining", 0, 1000, 0, 200)], 10) == "free"


def test_sql_capacity_concurrent_increase_and_pause_release(env):
    client, data = env["clients"]["a"], env["data"]["a"]
    # Isolate per-test remaining capacity from the shared module fixture.
    data.execute("UPDATE scheduler.capacity SET max_realtime_units=(SELECT sum(units)+2 FROM scheduler.tenant_load)")
    body = {"name": "capacity", "symbols": ["000001.SZ", "600000.SH"], "field": "price", "op": ">",
            "value": 9, "enabled": True}
    def save(pair):
        tenant, user = pair
        return client.post(path(env, tenant, "/monitor-rules"), headers=headers(env, user), json=body)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, [("alpha", "owner"), ("beta", "other")]))
    assert sorted(response.status_code for response in results) == [200, 429]
    assert next(r for r in results if r.status_code == 429).json()["detail"] == "cell_capacity_exceeded"
    winner = 0 if results[0].status_code == 200 else 1
    tenant, user = [("alpha", "owner"), ("beta", "other")][winner]
    rule = results[winner].json()["rule"]
    assert client.post(path(env, tenant, "/monitor-rules"), headers=headers(env, user),
                       json={**body, "id": rule["id"], "enabled": False}).status_code == 200
    loser = [("alpha", "owner"), ("beta", "other")][1-winner]
    assert save(loser).status_code == 200
    data.execute("UPDATE scheduler.capacity SET max_realtime_units=200")


def test_provisioning_limit_preserves_existing_mapping(env):
    control = env["control"]
    prior = control.execute("SELECT tenant_limit FROM control.cells WHERE id='a'").fetchone()["tenant_limit"]
    count = control.execute("SELECT count(*) AS n FROM control.tenants WHERE cell_id='a'").fetchone()["n"]
    control.execute("UPDATE control.cells SET tenant_limit=%s WHERE id='a'", (count,))
    try:
        with pytest.raises(ValueError, match="capacity"):
            provision_tenant(control, env["data"]["a"], "over-" + uuid.uuid4().hex, "over", "a", env["users"]["owner"])
        # Idempotent retries do not consume another tenant slot or change its Cell.
        provision_tenant(control, env["data"]["a"], env["tenants"]["alpha"], "alpha", "a", env["users"]["owner"])
        assert control.execute("SELECT cell_id FROM control.tenants WHERE id=%s", (env["tenants"]["alpha"],)).fetchone()["cell_id"] == "a"
    finally:
        control.execute("UPDATE control.cells SET tenant_limit=%s WHERE id='a'", (prior,))


def test_tenant_weight_limit_is_by_subscriptions_not_rule_count(env):
    client = env["clients"]["a"]
    data = env["data"]["a"]
    data.execute("UPDATE scheduler.capacity SET max_tenant_units=2")
    body = {"name": "weighted", "symbols": ["000001.SZ", "600000.SH", "600001.SH"],
            "field": "price", "op": ">", "value": 9}
    try:
        response = client.post(path(env, suffix="/monitor-rules"), headers=headers(env), json=body)
        assert response.status_code == 429
        assert client.get(path(env, suffix="/monitor-rules"), headers=headers(env)).json()["rules"] == []
        body["symbols"] = ["000001.SZ"]
        assert client.post(path(env, suffix="/monitor-rules"), headers=headers(env), json=body).status_code == 200
    finally:
        data.execute("UPDATE scheduler.capacity SET max_tenant_units=100")


def test_repacking_existing_tenant_does_not_double_count_or_mutate_route(env):
    tenant = env["tenants"]["alpha"]
    control = env["control"]
    control.execute("UPDATE control.tenants SET realtime_reservation=10 WHERE id=%s", (tenant,))
    tenants = [{"tenant_id": tenant, "realtime_units": 10}]
    before = control.execute("SELECT cell_id,routing_epoch FROM control.tenants WHERE id=%s", (tenant,)).fetchone()
    plan = split_plan(tenants, planning_loads(control, tenants))
    assert len(plan["assignments"]) == 1
    assert sum(c["realtime_units"] for c in plan["cells"]) == 10
    assert control.execute("SELECT cell_id,routing_epoch FROM control.tenants WHERE id=%s", (tenant,)).fetchone() == before
