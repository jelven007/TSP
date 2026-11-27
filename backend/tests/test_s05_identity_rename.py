"""A stable-ID rename retains frozen selection, monitoring and tenant isolation."""
import gzip
import json
from base64 import b64decode
from dataclasses import replace

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.admin import migrate
from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.services.daily_selections import DailySelections, selection_identity
from app.services.strategy_migration import migrate_strategy_identity
from app.services.strategy_results import StrategyResults
from app.strategy.version import digest, logic_version

OLD, NEW, DAY = "custom_173004552", "custom_mvxxcs05", "2026-11-26"


def test_current_s05_identity_and_scheduler_route():
    from app.services.selection_clock import S05_ID, job_key, selection_group, window_text
    from app.strategy.s02_173004552 import SID
    assert SID == S05_ID == NEW
    assert selection_group(NEW) == NEW
    assert job_key(DAY, NEW) == DAY + ":s05"
    assert window_text(NEW) == ("22:00:00", "23:00:00")


def seed(env):
    migrate(env["data"], "data", env["roles"][1], "full", target_version=7)
    env["db"].settings = env["db"].settings.model_copy(update={"schema_version": 7})
    _, user = register(env)
    docs = TenantDocuments(env["db"], env["identity"].context(user, None, "s05-rename"))
    code = f'META = {{"id": "{OLD}", "name": "s05"}}\n'
    docs.put("strategy_sources", OLD, {"id": OLD, "code": code, "source": "custom",
                                     "definition": {"meta": {"id": OLD}, "logic_version": logic_version(code)}})
    docs.put("strategy_monitoring", OLD, {"strategy_id": OLD, "enabled": True, "rule_id": "same-rule"})
    docs.put("strategy_pool_preferences", docs.context.user_id,
             {"ids": ["unrelated", OLD], "revision": 7})
    docs.put("monitor_results", "default", {"strategy_ids": [OLD], "updated_at": 987})
    docs.put("custom_strategy_status", digest(OLD)[:16] + ":" + DAY + ":stock:1m",
             {"status": "success", "removed_selection_symbols": []})
    from app.services.s05_selection import POLICY, WEIGHTS
    identity = replace(selection_identity(OLD, policy=POLICY), strategy_name="s05")
    snapshot = {"as_of": DAY, "source_date": "2026-11-25", "rows": [
        {"symbol": "600001.SH", "s02_score": 73.25}], "scoring_policy": POLICY,
        "scoring_weights": WEIGHTS, "identity": identity.fingerprint, "generated_at": "original-time"}
    record = StrategyResults(docs).save(identity, {
        "as_of": DAY, "strategy_id": OLD, "section_mode": "signals", "selection_only": True,
        "selection_snapshot": snapshot, "rows": snapshot["rows"],
        "sections": {"sell": [], "select": snapshot["rows"], "buy": []},
    }, started_at=123)
    pool = {"id": "original-pool", "strategy_id": OLD, "as_of": DAY,
            "symbols": ["600001.SH", "600002.SH"], "total": 2, "computed_at": 456}
    with docs._transaction(write=True) as conn:
        from psycopg.types.json import Jsonb
        conn.execute("INSERT INTO business.monitor_rules(tenant_id,id,definition) VALUES(%s,%s,%s)",
                     (docs.context.tenant_id, "same-rule", Jsonb({"strategy_id": OLD, "enabled": True})))
        conn.execute("INSERT INTO business.daily_stock_pools(tenant_id,id,strategy_id,day,started_at,value) "
                     "VALUES(%s,%s,%s,%s,%s,%s)",
                     (docs.context.tenant_id, pool["id"], OLD, DAY, 123, Jsonb(pool)))
    return docs, identity, record, pool


def test_pure_rename_retains_policy_slot_dates_typed_links_and_fences_old_writes(env):
    docs, identity, before, pool = seed(env)
    foreign, *_ = seed(env)
    old_record = docs.record("strategy_result_history", before["record_id"])
    receipt = migrate_strategy_identity(docs, OLD, NEW)
    assert receipt["renamed_records"] == 1
    saved = StrategyResults(docs).read(replace(identity, strategy_id=NEW), DAY)
    assert saved["summary"]["computed_at"] == before["summary"]["computed_at"]
    assert saved["summary"]["started_at"] == 123
    assert saved["result"]["selection_snapshot"]["rows"] == before["result"]["selection_snapshot"]["rows"]
    assert saved["result"]["selection_snapshot"]["identity"] == replace(identity, strategy_id=NEW).fingerprint
    after = docs.record("strategy_result_history", saved["record_id"])
    assert (after["revision"], after["updated_at"]) == (old_record["revision"], old_record["updated_at"])
    assert DailySelections(docs).read(NEW, DAY)["rows"] == before["result"]["selection_snapshot"]["rows"]
    assert docs.get("strategy_sources", OLD) is None
    source = docs.get("strategy_sources", NEW)
    assert OLD not in source["code"]
    assert source["definition"]["logic_version"] == logic_version(source["code"])
    assert docs.get("strategy_monitoring", NEW)["enabled"]
    assert docs.get("strategy_monitoring", NEW)["rule_id"] == "same-rule"
    assert docs.get("strategy_pool_preferences", docs.context.user_id) == {
        "ids": ["unrelated", NEW], "revision": 8}
    assert docs.get("monitor_results", "default") == {"strategy_ids": [NEW], "updated_at": 987}
    key = digest(NEW)[:16] + ":" + DAY + ":stock:1m"
    assert docs.get("custom_strategy_status", key)["status"] == "success"
    payload = docs.get("strategy_day_payload", key)
    assert json.loads(gzip.decompress(b64decode(payload["body"])))["result"]["strategy_id"] == NEW
    with docs._transaction() as conn:
        rule = conn.execute("SELECT definition FROM business.monitor_rules WHERE id='same-rule'").fetchone()
        current_pool = conn.execute("SELECT * FROM business.daily_stock_pools WHERE strategy_id=%s", (NEW,)).fetchone()
    assert rule["definition"] == {"strategy_id": NEW, "enabled": True}
    assert current_pool["value"]["symbols"] == pool["symbols"]
    assert current_pool["value"]["computed_at"] == 456
    assert migrate_strategy_identity(docs, OLD, NEW)["already_migrated"]
    assert StrategyResults(foreign).read(identity, DAY)
    with pytest.raises(AccessError):
        StrategyResults(docs).save(identity, {"as_of": DAY, "rows": []}, started_at=999)


def test_pure_rename_collision_rolls_back(env):
    docs, identity, _, _ = seed(env)
    docs.put("strategy_overrides", NEW, {"name": "existing"})
    with pytest.raises(AccessError, match="目标"):
        migrate_strategy_identity(docs, OLD, NEW)
    assert docs.get("strategy_sources", OLD)
    assert StrategyResults(docs).read(identity, DAY)
