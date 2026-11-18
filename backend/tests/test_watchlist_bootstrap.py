"""Column-only initialization through real provisioning transactions and RLS."""
# ruff: noqa: F811 -- shared pytest fixtures are imported for discovery.
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb
from test_full_identity import env, full_cluster, register, unprovisioned_admin  # noqa: F401

from app.cell.config import CellSettings
from app.cell.context import AccessError
from app.multitenancy.preferences import UserPreferences
from app.multitenancy.watchlist import Watchlist


def test_column_defaults_accept_current_deployed_schema_version():
    settings = CellSettings(id="full", schema_version=8, control_dsn="unused", data_dsn="unused")
    assert settings.schema_version == 8


@pytest.fixture
def defaults(env, tmp_path, monkeypatch):
    snapshot = {
        "format_version": 2,
        "columns": [
            {"id": "builtin:price", "label": "现价", "align": "center",
             "source": {"type": "builtin", "key": "price"}, "visible": True},
            {"id": "builtin:open", "label": "今开", "align": "right",
             "source": {"type": "builtin", "key": "open"}, "visible": False},
        ],
    }
    path = tmp_path / "watchlist-columns-defaults.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False))
    monkeypatch.setattr(env["db"], "settings", env["db"].settings.model_copy(
        update={"bootstrap_watchlist_columns_path": path},
    ))
    return path, snapshot


def service(env, user_id):
    with env["db"].control.connection() as conn:
        user = env["identity"]._public(conn, conn.execute(
            "SELECT * FROM control.users WHERE id=%s", (user_id,),
        ).fetchone())
    return Watchlist(env["db"], env["identity"].context(user, None, "defaults-test"))


def assert_no_seeded_business(env, user_id):
    watchlist = service(env, user_id)
    assert watchlist.list_symbols() == []
    assert watchlist.list_groups() == []
    tenant = env["control"].execute(
        "SELECT default_tenant_id FROM control.users WHERE id=%s", (user_id,),
    ).fetchone()["default_tenant_id"]
    assert env["data"].execute(
        """SELECT count(*) AS n FROM business.documents
           WHERE tenant_id=%s AND namespace='bootstrap_defaults'""", (tenant,),
    ).fetchone()["n"] == 0


def test_new_admin_gets_only_ordered_column_defaults(env, unprovisioned_admin, defaults):
    user_id = unprovisioned_admin["id"]
    assert env["identity"].ensure_admin_tenant(user_id)
    assert_no_seeded_business(env, user_id)
    assert UserPreferences(env["db"]).load(user_id)["watchlist_columns"] == defaults[1]["columns"]
    watchlist = service(env, user_id)
    watchlist.add("600000.SH")
    before = watchlist.list_symbols()
    defaults[0].unlink()
    env["identity"].provision(user_id)
    env["identity"].bootstrap_admin_tenants()
    assert watchlist.list_symbols() == before


@pytest.mark.parametrize("invalid", [
    {"format_version": 1, "groups": [], "items": [], "columns": []},
    {"format_version": 2, "columns": [], "items": [{"symbol": "600000.SH"}]},
    {"format_version": 2, "columns": [{"id": "builtin:price", "symbol": "600000.SH"}]},
    {"format_version": 2, "columns": [{"id": "builtin:price"}]},
])
def test_invalid_or_stock_bearing_defaults_leave_pending_and_retry(env, unprovisioned_admin, defaults, invalid):
    path, snapshot = defaults
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
    path.write_text(json.dumps(snapshot))
    env["identity"].provision(unprovisioned_admin["id"])
    assert_no_seeded_business(env, unprovisioned_admin["id"])
    assert UserPreferences(env["db"]).load(unprovisioned_admin["id"])["watchlist_columns"] == snapshot["columns"]


@pytest.mark.parametrize("saved", [[{"id": "builtin:price", "visible": False}], []])
def test_control_commit_retry_preserves_explicit_preferences(env, unprovisioned_admin, defaults, monkeypatch, saved):
    user_id = unprovisioned_admin["id"]
    env["control"].execute(
        "INSERT INTO control.user_preferences(user_id,value) VALUES(%s,%s)",
        (user_id, Jsonb({"watchlist_columns": saved, "nav_order": ["watchlist"]})),
    )
    original = env["identity"].provision
    monkeypatch.setattr(env["identity"], "provision", lambda _: None)
    env["identity"].ensure_admin_tenant(user_id)
    monkeypatch.setattr(env["identity"], "provision", original)
    connection = env["db"].control.connection
    calls = 0

    @contextmanager
    def fail_activation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interrupted control commit")
        with connection(*args, **kwargs) as conn:
            yield conn

    with monkeypatch.context() as patch:
        patch.setattr(env["db"].control, "connection", fail_activation)
        with pytest.raises(AccessError):
            original(user_id)
    original(user_id)
    assert_no_seeded_business(env, user_id)
    value = UserPreferences(env["db"]).load(user_id)
    assert value["watchlist_columns"] == saved
    assert value["nav_order"] == ["watchlist"]


def test_ordinary_registration_gets_columns_without_business_data(env, defaults):
    _, user = register(env)
    assert_no_seeded_business(env, user["id"])
    assert UserPreferences(env["db"]).load(user["id"])["watchlist_columns"] == defaults[1]["columns"]


def test_missing_preference_merges_without_overwriting_navigation(env, unprovisioned_admin, defaults):
    user_id = unprovisioned_admin["id"]
    env["control"].execute(
        "INSERT INTO control.user_preferences(user_id,value) VALUES(%s,%s)",
        (user_id, Jsonb({"watchlist_columns": None, "nav_order": ["watchlist"]})),
    )
    env["identity"].ensure_admin_tenant(user_id)
    value = UserPreferences(env["db"]).load(user_id)
    assert value["watchlist_columns"] == defaults[1]["columns"]
    assert value["nav_order"] == ["watchlist"]
    assert_no_seeded_business(env, user_id)


def test_complete_column_catalog_provisions_without_stocks(env, unprovisioned_admin, defaults):
    source = os.environ.get("TSP_TEST_WATCHLIST_COLUMNS_DEFAULTS")
    if not source:
        pytest.skip("requires a controlled column catalog")
    snapshot = json.loads(Path(source).read_text())
    assert set(snapshot) == {"format_version", "columns"}
    defaults[0].write_text(json.dumps(snapshot))
    user_id = unprovisioned_admin["id"]
    env["identity"].ensure_admin_tenant(user_id)
    assert_no_seeded_business(env, user_id)
    assert UserPreferences(env["db"]).load(user_id)["watchlist_columns"] == snapshot["columns"]
