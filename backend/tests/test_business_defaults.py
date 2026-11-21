"""Controlled business initialization must be explicit and administrator scoped."""
# ruff: noqa: F811 -- shared PostgreSQL fixtures.
import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from test_full_identity import env, full_cluster, register, unprovisioned_admin  # noqa: F401

from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.factors.registry import all_factors, private_factor_registry


def pack(payload):
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return {"format_version": 1, "snapshot_id": hashlib.sha256(raw.encode()).hexdigest(), "payload": payload}


class DescribeExecutor:
    def __init__(self, definition):
        self.definition, self.calls = definition, []

    def execute(self, payload):
        assert payload["kind"] == "describe"
        self.calls.append(payload)
        return {"definition": self.definition}


@pytest.fixture
def defaults(env, unprovisioned_admin, tmp_path, monkeypatch):
    from app.strategy.engine import StrategyEngine

    engine = StrategyEngine([Path(__file__).resolve().parents[1] / "app/strategy/builtin"])
    from app.multitenancy.strategies import definition_payload

    definition = definition_payload(engine.strategy_definitions()[0])
    definition["meta"] = {**definition["meta"], "id": "custom_init", "name": "初始化策略"}
    definition["source"] = "custom"
    definition["required_features"] = ["csg_default_entry"]
    definition["entry_signals"] = ["csg_default_entry"]
    definition["basic_filter"] = {"target_mode": "all"}
    with private_factor_registry([], strict=True):
        catalog = json.loads(json.dumps([asdict(s) for s in all_factors()],
                                       default=lambda v: sorted(v) if isinstance(v, frozenset) else v))
    payload = {
        "target_username": unprovisioned_admin["username"],
        "source": {"version": "test", "formal_sha": "fixture", "patch_id": "fixture"},
        "documents": [
            {"namespace": "strategy_sources", "id": "custom_init", "value": {
                "id": "custom_init", "source": "custom", "code": "# remote only", "definition": definition}},
            {"namespace": "strategy_overrides", "id": "custom_init",
             "value": {"basic_filter": {"target_mode": "all"}, "stop_loss": None,
                       "entry_signals": ["csg_default_entry"]}},
            {"namespace": "custom_signals", "id": "default_entry", "value": {
                "id": "default_entry", "name": "入场", "enabled": True, "kind": "entry",
                "timeframe": "daily", "conditions": [{"left": "close", "op": ">", "right": 0}]}},
            {"namespace": "strategy_monitoring", "id": "custom_init", "value": {
                "strategy_id": "custom_init", "enabled": True, "rule_id": "mr_init",
                "contract": "fixture"}},
            {"namespace": "strategy_pool_preferences", "id": "default", "value": {"ids": ["custom_init"]}},
        ],
        "monitor_rules": [{"id": "mr_init", "enabled": True, "definition": {
            "id": "mr_init", "type": "strategy", "strategy_id": "custom_init", "enabled": True}}],
        "factor_catalog": catalog, "code_hashes": {},
        "backtest": {"last": {"defaultsVersion": 5, "initialCapital": "1000000",
                             "minimumCommission": "5"}, "quick_ranges": []},
    }
    snapshot = pack(payload)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False))
    executor = DescribeExecutor(definition)
    monkeypatch.setattr(env["identity"], "business_defaults_executor", executor, raising=False)
    monkeypatch.setattr(env["db"], "settings", env["db"].settings.model_copy(
        update={"bootstrap_business_defaults_path": path},
    ))
    return path, snapshot, executor


def rows(env, user_id):
    tenant = env["control"].execute(
        "SELECT default_tenant_id FROM control.users WHERE id=%s", (user_id,),
    ).fetchone()["default_tenant_id"]
    return env["data"].execute(
        "SELECT namespace,id,value FROM business.documents WHERE tenant_id=%s", (tenant,),
    ).fetchall()


def test_admin_seed_is_atomic_and_keeps_null_and_rebuilds_monitor_identity(env, unprovisioned_admin, defaults):
    uid = unprovisioned_admin["id"]
    assert env["identity"].ensure_admin_tenant(uid)
    saved = {(r["namespace"], r["id"]): r["value"] for r in rows(env, uid)}
    assert saved["strategy_overrides", "custom_init"]["stop_loss"] is None
    assert saved["custom_signals", "default_entry"]["id"] == "default_entry"
    assert saved["strategy_monitoring", "custom_init"]["generation"] == 1
    assert saved["strategy_pool_preferences", uid] == {
        "ids": ["custom_init"], "revision": 1, "legacy_import": False,
    }
    assert saved["bootstrap_business_defaults", "default"]["snapshot_id"] == defaults[1]["snapshot_id"]
    tenant = env["control"].execute("SELECT * FROM control.users WHERE id=%s", (uid,)).fetchone()
    rule = env["data"].execute(
        "SELECT * FROM business.monitor_rules WHERE tenant_id=%s", (tenant["default_tenant_id"],),
    ).fetchone()
    assert rule["version"] == 1 and rule["enabled"]
    assert rule["definition"]["_execution"] == {
        "user_id": uid, "auth_version": tenant["auth_version"], "policy_version": 1, "routing_epoch": 1,
    }
    assert len(defaults[2].calls) == 1


def test_other_accounts_never_read_or_receive_admin_defaults(env, unprovisioned_admin, defaults):
    _, user = register(env)
    assert {r["namespace"] for r in rows(env, user["id"])} == {"preferences"}
    env["client"].cookies.set("tf_session", env["identity"]._session(user["id"])[0])
    response = env["client"].get("/api/business/initialization-defaults")
    assert response.status_code == 200
    assert response.json() == {"snapshot_id": None, "backtest": None}


def test_designated_admin_reads_installed_defaults_without_rewriting_existing_data(
    env, unprovisioned_admin, defaults,
):
    uid = unprovisioned_admin["id"]
    path, snapshot, _ = defaults
    settings = env["db"].settings
    env["db"].settings = settings.model_copy(update={"bootstrap_business_defaults_path": None})
    try:
        env["identity"].ensure_admin_tenant(uid)
    finally:
        env["db"].settings = settings
    before = rows(env, uid)
    env["client"].cookies.set("tf_session", env["identity"]._session(uid)[0])
    response = env["client"].get("/api/business/initialization-defaults")
    assert response.status_code == 200
    assert response.json()["backtest"] == snapshot["payload"]["backtest"]
    assert rows(env, uid) == before
    path.unlink()
    env["identity"].provision(uid)
    assert rows(env, uid) == before


def test_seeded_admin_reads_durable_defaults_after_file_is_missing(env, unprovisioned_admin, defaults):
    uid = unprovisioned_admin["id"]
    env["identity"].ensure_admin_tenant(uid)
    defaults[0].unlink()
    env["client"].cookies.set("tf_session", env["identity"]._session(uid)[0])
    response = env["client"].get("/api/business/initialization-defaults")
    assert response.status_code == 200
    assert response.json()["snapshot_id"] == defaults[1]["snapshot_id"]


def test_conflict_rolls_back_all_default_writes(env, unprovisioned_admin, defaults, monkeypatch):
    from app.multitenancy.business_defaults import load_snapshot, seed_business

    uid = unprovisioned_admin["id"]
    with monkeypatch.context() as patch:
        patch.setattr(env["identity"], "provision", lambda _: None)
        env["identity"].ensure_admin_tenant(uid)
    job = env["control"].execute(
        "SELECT j.*,t.routing_epoch,t.policy_version,u.auth_version FROM control.provisioning_jobs j "
        "JOIN control.tenants t ON t.id=j.tenant_id JOIN control.users u ON u.id=j.user_id WHERE j.user_id=%s",
        (uid,),
    ).fetchone()
    env["data"].execute("SELECT business.initialize_tenant(%s,%s)", (job["tenant_id"], 1))
    env["data"].execute(
        "INSERT INTO business.documents(tenant_id,namespace,id,value) VALUES(%s,'custom_signals','default_entry',%s)",
        (job["tenant_id"], Jsonb({"user_owned": True})),
    )
    with pytest.raises(UniqueViolation), env["db"].data.connection() as conn, conn.transaction():
        conn.execute("SELECT business.initialize_tenant(%s,%s)", (job["tenant_id"], 1))
        seed_business(conn, job, load_snapshot(defaults[0]))
    saved = env["data"].execute(
        "SELECT namespace,id,value FROM business.documents WHERE tenant_id=%s", (job["tenant_id"],),
    ).fetchall()
    assert {r["namespace"] for r in saved} == {"preferences", "custom_signals"}
    assert next(r["value"] for r in saved if r["namespace"] == "custom_signals") == {"user_owned": True}


@pytest.mark.parametrize("change", ["digest", "namespace", "duplicate", "signal", "factor", "catalog", "group", "source"])
def test_invalid_pack_leaves_no_partial_tenant_data(env, unprovisioned_admin, defaults, change):
    path, snapshot, executor = defaults
    payload = snapshot["payload"]
    if change == "namespace":
        payload["documents"].append({"namespace": "paper_accounts", "id": "x", "value": {}})
    elif change == "duplicate":
        payload["documents"].append(payload["documents"][0])
    elif change == "signal":
        payload["documents"] = [r for r in payload["documents"] if r["namespace"] != "custom_signals"]
    elif change == "factor":
        payload["documents"][1]["value"]["scoring"] = {"missing_factor": 1}
    elif change == "catalog":
        payload["factor_catalog"].pop()
    elif change == "group":
        payload["documents"][1]["value"]["basic_filter"] = {
            "target_mode": "selected", "boards": [], "include_custom": True, "watchlist_group_ids": ["missing"]}
    elif change == "source":
        executor.definition = {**executor.definition, "source": "ai"}
    invalid = pack(payload)
    if change == "digest":
        invalid["snapshot_id"] = "0" * 64
    path.write_text(json.dumps(invalid))
    with pytest.raises(AccessError, match="初始化"):
        env["identity"].ensure_admin_tenant(unprovisioned_admin["id"])
    job = env["control"].execute(
        "SELECT * FROM control.provisioning_jobs WHERE user_id=%s", (unprovisioned_admin["id"],),
    ).fetchone()
    assert job["state"] == "pending"
    assert env["data"].execute(
        "SELECT 1 FROM business.tenant_bindings WHERE tenant_id=%s", (job["tenant_id"],),
    ).fetchone() is None


def test_control_commit_retry_does_not_reseed_changed_or_deleted_resources(
    env, unprovisioned_admin, defaults, monkeypatch,
):
    uid = unprovisioned_admin["id"]
    provision = env["identity"].provision
    with monkeypatch.context() as patch:
        patch.setattr(env["identity"], "provision", lambda _: None)
        env["identity"].ensure_admin_tenant(uid)
    connection = env["db"].control.connection
    calls = 0

    @contextmanager
    def fail_activation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("interrupted activation")
        with connection(*args, **kwargs) as conn:
            yield conn

    with monkeypatch.context() as patch:
        patch.setattr(env["db"].control, "connection", fail_activation)
        with pytest.raises(AccessError):
            provision(uid)
    job = env["control"].execute(
        "SELECT * FROM control.provisioning_jobs WHERE user_id=%s", (uid,),
    ).fetchone()
    env["data"].execute(
        "DELETE FROM business.documents WHERE tenant_id=%s AND namespace='custom_signals'", (job["tenant_id"],),
    )
    env["data"].execute(
        """UPDATE business.documents SET value=%s
           WHERE tenant_id=%s AND namespace='strategy_overrides'""",
        (Jsonb({"stop_loss": 0.17}), job["tenant_id"]),
    )
    provision(uid)
    saved = {(r["namespace"], r["id"]): r["value"] for r in rows(env, uid)}
    assert ("custom_signals", "default_entry") not in saved
    assert saved["strategy_overrides", "custom_init"]["stop_loss"] == 0.17
    defaults[0].unlink()
    provision(uid)
    assert saved == {(r["namespace"], r["id"]): r["value"] for r in rows(env, uid)}


def test_controlled_business_defaults_path_is_preserved():
    path = Path("/controlled/business-defaults/manifest.json")
    settings = CellSettings(
        id="full", control_dsn="unused", data_dsn="unused",
        bootstrap_business_defaults_path=path,
    )
    assert settings.model_dump().get("bootstrap_business_defaults_path") == path


def test_old_configuration_has_no_business_seed():
    settings = CellSettings(id="full", control_dsn="unused", data_dsn="unused")
    assert settings.model_dump().get("bootstrap_business_defaults_path") is None
