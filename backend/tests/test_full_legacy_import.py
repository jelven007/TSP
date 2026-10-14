"""Real PostgreSQL import reconciliation, rollback and archived-file isolation."""
import json
import uuid
from datetime import UTC, datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import InsufficientPrivilege
from test_full_identity import PASSWORD, register
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster

from app.cell.admin import import_users, provision_tenant, set_tenant_status
from app.cell.passwords import hash_password
from app.multitenancy import access, legacy_archive, legacy_import
from app.multitenancy.paper_storage import TenantPaper
from app.strategy import paper


@pytest.fixture
def legacy(env, tmp_path):
    root = tmp_path / "legacy"
    private = root / "user_data"
    private.mkdir(parents=True)
    user_id, tenant_id = uuid.uuid4().hex, uuid.uuid4().hex
    salt, password_hash = hash_password(PASSWORD)
    secret = env["control"].execute("SELECT * FROM control.registration_config").fetchone()
    (private / "auth.json").write_text(json.dumps({
        "users": {user_id: {"id": user_id, "email": user_id + "@example.test",
                           "password_salt": salt, "password_hash": password_hash}},
        "sessions": {"obsolete": {"token": "never-copy-session"}},
        "registration_secret_salt": secret["secret_salt"],
        "registration_secret_hash": secret["secret_hash"],
    }))
    (private / "preferences.json").write_text(json.dumps({
        "watchlist_columns": [{"key": "symbol", "visible": True}],
        "email_smtp_config": {"host": "private-platform-config"},
        "review_schedule": {"enabled": True, "hour": 15, "minute": 40},
        "mining_schedule_enabled": False,
    }))
    (private / "secrets.json").write_text('{"api_key":"never-archive-credentials"}')
    (private / "strategy_cache.json").write_text('{"cached":"rebuild-this"}')
    (private / "ai_stock_reports.json").write_text(json.dumps([
        {"id": "original", "content": "原始个股报告", "created_at": "2026-10-12"},
    ]))
    (private / "watchlist_groups.json").write_text(json.dumps([
        {"id": "g1", "name": "原分组", "color": "rose"},
    ]))
    pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"], "note": ["旧备注", "另一备注"],
        "added_at": ["2026-10-01T10:00:00+08:00"] * 2, "group_ids": [["g1"], []],
    }).write_parquet(private / "watchlist.parquet")
    (private / "alerts.jsonl").write_text(json.dumps(
        {"ts": 1791864000000, "symbol": "600000.SH", "message": "原告警"},
    ) + "\n")
    overrides = private / "strategy_overrides"
    overrides.mkdir()
    (overrides / "n_day_low_reversal.json").write_text('{"params":{"lookback_days":60}}')
    manifest = root / "backtest_results" / "manifests" / "trace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"run_id":"trace","trade_count":15,"stats_hash":"original-hash"}')
    import_users(env["control"], private, user_id)
    provision_tenant(env["control"], env["data"], tenant_id, "原账户", "full", user_id)
    set_tenant_status(env["control"], tenant_id, "suspended", user_id)
    return root, tenant_id, user_id


def run_import(env, legacy):
    root, tenant_id, user_id = legacy
    return legacy_import.import_full_product(env["control"], env["data"], root, tenant_id, user_id)


def activate(env, legacy):
    root, tenant_id, user_id = legacy
    set_tenant_status(env["control"], tenant_id, "active", user_id)
    token, user = env["identity"].login(user_id + "@example.test", PASSWORD, uuid.uuid4().hex)
    context = env["identity"].context(user, None, "legacy-test")
    return token, TenantPaper(env["db"], context, root)


def test_full_import_retains_content_and_password_with_private_originals(env, legacy):
    result = run_import(env, legacy)
    root, tenant_id, _user_id = legacy
    assert result["reconciled"] and result["rows"]["watchlist_items"] == 2
    assert result["rows"]["documents"] == 5
    assert legacy_import.verify_import(env["data"], root, tenant_id)["source_hash"] == result["source_hash"]
    original_hash = result["source_hash"]
    assert run_import(env, legacy)["source_hash"] == original_hash
    token, owned = activate(env, legacy)
    assert owned.get("ai_reports", "ai_stock_reports.json")[0]["content"] == "原始个股报告"
    assert owned.get("strategy_overrides", "n_day_low_reversal")["params"]["lookback_days"] == 60
    schedule = owned.get("review_schedule", "default")
    assert schedule["policy_version"] == owned.context.policy_version
    assert schedule["auth_version"] == env["identity"].current_user(token)["auth_version"]
    with owned._transaction() as conn:
        names = [row["path"] for row in conn.execute("SELECT path FROM business.legacy_artifacts").fetchall()]
        assert "backtest_results/manifests/trace.json" in names
        assert all(not any(term in name for term in ("auth", "secret", "preferences", "strategy_cache")) for name in names)
        assert [row["symbol"] for row in conn.execute(
            "SELECT symbol FROM business.watchlist_items ORDER BY position",
        ).fetchall()] == ["600000.SH", "000001.SZ"]
        with pytest.raises(InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT * FROM migration.full_import_records")
    app = FastAPI()
    app.state.account_identity = env["identity"]
    app.middleware("http")(access.middleware)
    app.add_exception_handler(access.AccessError, access.access_error)
    app.include_router(legacy_archive.router)
    with TestClient(app) as client:
        client.cookies.set("tf_session", token)
        items = client.get("/api/backtest/legacy").json()["items"]
        item = next(row for row in items if row["path"].endswith("trace.json"))
        download = client.get("/api/backtest/legacy/download", params={"path": item["path"], "sha256": item["sha256"]})
        assert download.content == (root / item["path"]).read_bytes()
        assert download.headers["cache-control"] == "no-store"
        assert client.get("/api/backtest/legacy/download", params={
            "path": item["path"], "sha256": "0" * 64,
        }).status_code == 409
        other, _ = register(env)
        client.cookies.set("tf_session", other)
        assert client.get("/api/backtest/legacy").json()["items"] == []
        assert client.get("/api/backtest/legacy/download", params={
            "path": item["path"], "sha256": item["sha256"],
        }).status_code == 404
    assert env["control"].execute(
        "SELECT count(*) AS n FROM control.sessions WHERE digest='obsolete'",
    ).fetchone()["n"] == 0


def test_increment_updates_only_owned_records_and_reconciles_deletion(env, legacy):
    run_import(env, legacy)
    root, tenant_id, _ = legacy
    private = root / "user_data"
    rows = pl.read_parquet(private / "watchlist.parquet").to_dicts()
    rows[0]["note"] = "最终增量备注"
    pl.DataFrame(rows).write_parquet(private / "watchlist.parquet")
    (private / "ai_stock_reports.json").unlink()
    result = run_import(env, legacy)
    assert result["reconciled"]
    assert env["data"].execute(
        "SELECT note FROM business.watchlist_items WHERE tenant_id=%s AND symbol='600000.SH'", (tenant_id,),
    ).fetchone()["note"] == "最终增量备注"
    assert not env["data"].execute(
        "SELECT 1 FROM business.documents WHERE tenant_id=%s AND namespace='ai_reports'", (tenant_id,),
    ).fetchone()
    assert not env["data"].execute(
        "SELECT 1 FROM business.legacy_artifacts WHERE tenant_id=%s AND path='user_data/ai_stock_reports.json'",
        (tenant_id,),
    ).fetchone()
    assert legacy_import.verify_import(env["data"], root, tenant_id)["reconciled"]


def test_preview_edit_refuses_overwrite_and_rolls_back_identity(env, legacy):
    run_import(env, legacy)
    root, tenant_id, user_id = legacy
    version = env["control"].execute(
        "SELECT auth_version FROM control.users WHERE id=%s", (user_id,),
    ).fetchone()["auth_version"]
    env["data"].execute(
        "UPDATE business.watchlist_items SET note='preview-edit' WHERE tenant_id=%s AND symbol='600000.SH'",
        (tenant_id,),
    )
    with pytest.raises(ValueError, match="destination changed"):
        run_import(env, legacy)
    assert env["control"].execute(
        "SELECT auth_version FROM control.users WHERE id=%s", (user_id,),
    ).fetchone()["auth_version"] == version
    with pytest.raises(ValueError, match="content differs"):
        legacy_import.verify_import(env["data"], root, tenant_id)


def test_mid_import_failure_rolls_back_business_identity_and_retries(env, legacy, monkeypatch):
    _, tenant_id, user_id = legacy
    version = env["control"].execute(
        "SELECT auth_version FROM control.users WHERE id=%s", (user_id,),
    ).fetchone()["auth_version"]
    original = legacy_import.apply_plan
    def fail(*args):
        original(*args)
        raise ValueError("injected commit failure")
    with monkeypatch.context() as patch:
        patch.setattr(legacy_import, "apply_plan", fail)
        with pytest.raises(ValueError, match="injected"):
            run_import(env, legacy)
    assert not env["data"].execute(
        "SELECT 1 FROM business.legacy_artifacts WHERE tenant_id=%s", (tenant_id,),
    ).fetchone()
    assert not env["data"].execute(
        "SELECT 1 FROM migration.full_import_batches WHERE tenant_id=%s", (tenant_id,),
    ).fetchone()
    assert env["control"].execute(
        "SELECT auth_version FROM control.users WHERE id=%s", (user_id,),
    ).fetchone()["auth_version"] == version
    assert run_import(env, legacy)["reconciled"]


def test_original_paper_ledger_replay_and_immutable_import(env, legacy, monkeypatch):
    root, tenant_id, _ = legacy
    monkeypatch.setattr(paper, "cn_now", lambda: datetime(2026, 10, 14, 2, tzinfo=UTC))
    paper.create_account(root, 100000)
    order, error = paper.create_order(root, "600000.SH", "buy", qty=200, ref_price=10)
    assert error is None
    paper.evaluate_intraday(root, {"600000.SH": 10})
    assert paper.load_fills(root)[0]["order_id"] == order["id"]
    result = run_import(env, legacy)
    assert result["rows"]["full_paper_fills"] == 1
    assert run_import(env, legacy)["reconciled"]
    token, owned = activate(env, legacy)
    assert paper.get_account(owned) == paper.get_account(root)
    assert paper.load_orders(owned) == paper.load_orders(root)
    assert paper.load_fills(owned) == paper.load_fills(root)
    assert paper.replay_positions(owned) == paper.replay_positions(root)
    set_tenant_status(env["control"], tenant_id, "suspended", owned.context.user_id)
    fills = root / "paper" / "accounts" / "default" / "fills.jsonl"
    old = json.loads(fills.read_text())
    fills.write_text(json.dumps({**old, "price": 20}) + "\n")
    with pytest.raises(ValueError, match="immutable ledger"):
        run_import(env, legacy)
    assert token


def test_source_mutation_and_unclassified_data_fail_closed(env, legacy, monkeypatch):
    root, tenant_id, _ = legacy
    (root / "user_data" / "unknown_private.json").write_text("{}")
    with pytest.raises(ValueError, match="unclassified private"):
        run_import(env, legacy)
    (root / "user_data" / "unknown_private.json").unlink()
    original = legacy_import.apply_plan
    def mutate(*args):
        counts = original(*args)
        (root / "user_data" / "ai_stock_reports.json").write_text("[]")
        return counts
    monkeypatch.setattr(legacy_import, "apply_plan", mutate)
    with pytest.raises(ValueError, match="source changed"):
        run_import(env, legacy)
    assert not env["data"].execute(
        "SELECT 1 FROM migration.full_import_batches WHERE tenant_id=%s", (tenant_id,),
    ).fetchone()


def test_existing_cell_mail_channel_requires_explicit_same_tenant_mapping(env, legacy):
    root, tenant_id, user_id = legacy
    path = root / "user_data" / "preferences.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "webhook_default_channels": ["email"]}))
    env["data"].execute(
        """INSERT INTO business.notification_channels
           (tenant_id,id,name,kind,destination,enabled,user_id,policy_version,routing_epoch)
           VALUES(%s,'legacy-email','原邮件','email',%s,true,%s,1,1)""",
        (tenant_id, user_id + "@example.test", user_id),
    )
    with pytest.raises(ValueError, match="explicit channel reconciliation"):
        run_import(env, legacy)
    with pytest.raises(ValueError, match="missing or mismatched"):
        legacy_import.import_full_product(
            env["control"], env["data"], root, tenant_id, user_id, channel_map={"email": "other-tenant"},
        )
    result = legacy_import.import_full_product(
        env["control"], env["data"], root, tenant_id, user_id, channel_map={"email": "legacy-email"},
    )
    assert result["channel_map"] == {"email": "legacy-email"}
    token, owned = activate(env, legacy)
    assert owned.get("notification_preferences", "default")["default_channels"] == ["legacy-email"]
    channel = env["data"].execute(
        "SELECT * FROM business.notification_channels WHERE tenant_id=%s", (tenant_id,),
    ).fetchone()
    assert channel["auth_version"] == env["identity"].current_user(token)["auth_version"]
    assert channel["policy_version"] == owned.context.policy_version
