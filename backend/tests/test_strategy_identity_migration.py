"""Strategy retirement/rename must preserve financial history and fence old writers."""
import json
from dataclasses import replace
from importlib import import_module

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.research import ResearchJobs
from app.services.strategy_results import ResultIdentity, StrategyResults, previous_daily_state
from app.strategy.engine import StrategyEngine


def migrate(*args):
    return import_module("app.services.strategy_migration").migrate_strategy_identity(*args)


def setup(env):
    _, user = register(env)
    docs = TenantDocuments(env["db"], env["identity"].context(user, None, "identity-migration"))
    state = {"as_of": "2026-08-24", "cash": 110000., "nav": 110000., "positions": [], "complete": True}
    old = ResultIdentity("old_minute", "old_minute · 净6% / 毛6%", "original-version", json.dumps({"initial_cash": 100000}),
                         "{}", "null", "stock", "1m", daily_record=True)
    store = StrategyResults(docs)
    record = store.save(old, {"as_of": "2026-08-24", "strategy_id": old.strategy_id,
                             "sections": {"sell": [], "select": [], "buy": []}, "state": state},
                        started_at=10)
    removed = replace(old, strategy_id="removed_minute")
    store.save(removed, {"as_of": "2026-08-24", "rows": []}, started_at=11)
    docs.put("strategy_overrides", old.strategy_id, {"name": old.strategy_name, "params": {"target": .06}})
    docs.put("strategy_overrides", removed.strategy_id, {"params": {"target": .05}})
    docs.put("monitor_rules", "retained", {"id": "retained", "strategy_id": old.strategy_id, "enabled": True})
    docs.put("monitor_rules", "retired", {"id": "retired", "strategy_id": removed.strategy_id})
    docs.put("strategy_cache", "default", {"results": {old.strategy_id: {"rows": []}, removed.strategy_id: {"rows": []}}})
    jobs = ResearchJobs(docs)
    payload = {"kind": "backtest", "tenant_id": docs.context.tenant_id, "config": {"strategy_id": old.strategy_id},
               "sources": [], "overrides": {old.strategy_id: {"name": old.strategy_name}, removed.strategy_id: {}}}
    keep = jobs.submit(payload, force=True)
    delete = jobs.submit({**payload, "config": {"strategy_id": removed.strategy_id}}, force=True)
    with docs._transaction(write=True) as conn:
        from psycopg.types.json import Jsonb
        conn.execute("UPDATE business.research_jobs SET state='succeeded',result=%s WHERE id=%s",
                     (Jsonb({"stats": {"total_return": .1}, "config": payload["config"],
                             "strategy_info": {"id": old.strategy_id, "name": old.strategy_name},
                             "trades": [{"symbol": "600519.SH", "pnl_amount": 10000}],
                             "provenance": {"source_hash": "original-code",
                                            "sources": {"removed_minute.py": "removed-code",
                                                        "old_minute.py": "retained-code"}}}), keep["id"]))
        conn.execute("UPDATE business.research_jobs SET state='succeeded' WHERE id=%s", (delete["id"],))
        conn.execute("INSERT INTO business.research_artifacts(tenant_id,job_id,name,content) VALUES(%s,%s,%s,%s)",
                     (docs.context.tenant_id, delete["id"], "details", b"retired-artifact"))
    return docs, store, jobs, old, record, keep, delete, state


def test_sql_rename_rekeys_history_jobs_settings_and_preserves_financial_values(env):
    docs, store, jobs, old, record, keep, delete, state = setup(env)
    migrate(docs, old.strategy_id, "new_minute", "removed_minute")
    current = replace(old, strategy_id="new_minute", strategy_name="new_minute · 净6% / 毛6%")
    assert store.list_history(old.strategy_id)["records"] == []
    assert store.list_history("removed_minute")["records"] == []
    new = store.read(current, "2026-08-24")
    assert new["result"]["state"] == state
    assert new["summary"]["computed_at"] == record["summary"]["computed_at"]
    assert new["summary"]["started_at"] == 10
    assert new["summary"]["strategy_version"] == "original-version"
    assert new["summary"]["identity"] == current.fingerprint
    assert previous_daily_state(docs, "new_minute", "2026-08-25") == state
    assert docs.get("strategy_overrides", old.strategy_id) is None
    assert docs.get("strategy_overrides", "new_minute")["params"] == {"target": .06}
    assert docs.get("monitor_rules", "retired") is None
    assert docs.get("monitor_rules", "retained")["strategy_id"] == "new_minute"
    assert docs.get("strategy_cache", "default") is None
    saved = jobs.get(keep["id"])
    assert saved["input"]["config"]["strategy_id"] == "new_minute"
    assert saved["result"]["strategy_info"]["id"] == "new_minute"
    assert saved["result"]["stats"] == {"total_return": .1}
    assert saved["result"]["trades"] == [{"symbol": "600519.SH", "pnl_amount": 10000}]
    assert saved["result"]["provenance"] == {
        "source_hash": "original-code", "sources": {"new_minute.py": "retained-code"},
    }
    assert jobs.get(delete["id"]) is None
    with pytest.raises(AccessError):
        jobs.artifact(delete["id"], "details")
    with pytest.raises(AccessError):
        store.save(old, {"as_of": "2026-08-25", "rows": []}, started_at=12)
    migrate(docs, old.strategy_id, "new_minute", "removed_minute")
    assert len(store.list_history("new_minute")["records"]) == 1


def test_sql_migration_is_tenant_scoped_and_rolls_back_on_collision(env):
    docs, store, jobs, old, _record, _keep, delete, state = setup(env)
    foreign, *_ = setup(env)
    docs.put("strategy_overrides", "new_minute", {"params": {"target": .9}})
    with pytest.raises(AccessError) as error:
        migrate(docs, old.strategy_id, "new_minute", "removed_minute")
    assert error.value.status == 409
    assert store.read(old, "2026-08-24")["result"]["state"] == state
    assert jobs.get(delete["id"]) is not None
    docs.delete("strategy_overrides", "new_minute")
    migrate(docs, old.strategy_id, "new_minute", "removed_minute")
    assert StrategyResults(foreign).read(old, "2026-08-24") is not None


def test_sql_migration_rejects_active_jobs_before_any_change(env):
    docs, store, _jobs, old, _record, keep, _delete, _state = setup(env)
    with docs._transaction(write=True) as conn:
        conn.execute("UPDATE business.research_jobs SET state='running' WHERE id=%s", (keep["id"],))
    with pytest.raises(AccessError, match="任务") as error:
        migrate(docs, old.strategy_id, "new_minute", "removed_minute")
    assert error.value.status == 409
    assert store.read(old, "2026-08-24") is not None
    assert docs.get("strategy_overrides", old.strategy_id) is not None


def test_sql_migration_preserves_document_dates_and_rejects_record_key_collision(env):
    docs, store, _jobs, old, record, *_ = setup(env)
    docs.put("monitor_rules", "rule_old_minute", {"strategy_id": old.strategy_id})
    docs.put("monitor_rules", "rule_new_minute", {"strategy_id": "unrelated"})
    with pytest.raises(AccessError, match="记录键"):
        migrate(docs, old.strategy_id, "new_minute", "removed_minute")
    assert docs.get("monitor_rules", "rule_new_minute") == {"strategy_id": "unrelated"}
    assert store.read(old, "2026-08-24") is not None
    docs.delete("monitor_rules", "rule_new_minute")
    with docs._transaction() as conn:
        before = conn.execute("SELECT revision,updated_at FROM business.documents WHERE id=%s",
                              (record["summary"]["record_id"],)).fetchone()
    migrate(docs, old.strategy_id, "new_minute", "removed_minute")
    current = store.list_history("new_minute")["records"][0]
    with docs._transaction() as conn:
        after = conn.execute("SELECT revision,updated_at FROM business.documents WHERE id=%s",
                             (current["record_id"],)).fetchone()
    assert after == before


def test_builtin_catalog_preserves_shared_id_and_configured_retirement(monkeypatch):
    from pathlib import Path

    from app.config import settings

    monkeypatch.setattr(settings, "retired_builtin_strategies", [])
    engine = StrategyEngine([Path(__file__).parents[1] / "app/strategy/builtin"])
    assert engine.has("s02")
    assert not engine.has("s021") and not engine.has("s022")
    assert engine.get("s01").meta["timeframes"] == ["1m"]
    monkeypatch.setattr(settings, "retired_builtin_strategies", ["s02"])
    engine.reload()
    assert not engine.has("s02")
    assert engine.has("s01")
