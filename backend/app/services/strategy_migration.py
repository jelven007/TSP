"""Tenant-scoped strategy identity migration, with atomic history and job updates."""
from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.research import ResearchJobs, signature
from app.services.strategy_results import (
    LIFECYCLE_NAMESPACE,
    NAMESPACE,
    ResultIdentity,
    StrategyResults,
    clear_memory,
)
from app.services.task_state import ACTIVE_TASK_STATUSES
from app.strategy.version import digest

_DROP = object()


def _rewrite(value, old_id, new_id, removed_id):
    if isinstance(value, str):
        return _DROP if value == removed_id else value.replace(old_id, new_id)
    if isinstance(value, list):
        rewritten = [_rewrite(item, old_id, new_id, removed_id) for item in value]
        return [item for item in rewritten if item is not _DROP]
    if isinstance(value, dict):
        if removed_id is not None and (value.get("strategy_id") == removed_id or value.get("id") == removed_id):
            return _DROP
        result = {}
        for key, item in value.items():
            if removed_id is not None and removed_id in key:
                continue
            item = _rewrite(item, old_id, new_id, removed_id)
            if item is not _DROP:
                result[key.replace(old_id, new_id)] = item
        return result
    return value


def _rename_only(documents, old_id, new_id):
    """Normalize mutable labels; retain executions and serialize with old writers."""
    from psycopg.types.json import Jsonb

    from app.services.strategy_day_views import publish
    from app.strategy.version import logic_version

    namespaces = [
        "strategy_sources", "strategy_overrides", "strategy_monitoring",
        "strategy_execution_status", "strategy_monitor_task_runs", "monitor_rules",
        "monitor_runtime", "monitor_results", "strategy_cache", "daily_selection_jobs", "daily_stock_pool_jobs",
        "strategy_monitor_tasks", "strategy_task_receipts", "preferences",
        "paper_auto", "trade_automations", "strategy_pool_preferences",
    ]
    prefixes = [digest(sid)[:16] + ":" for sid in (old_id, new_id)]
    with documents.transaction() as session:
        conn = session.connection
        records = conn.execute(
            """SELECT * FROM business.documents WHERE id LIKE %s OR id LIKE %s
               OR id LIKE %s OR id LIKE %s
               OR (namespace=ANY(%s) AND (value::text LIKE %s OR value::text LIKE %s))
               ORDER BY namespace,id""",
            ("%" + old_id + "%", "%" + new_id + "%", prefixes[0] + "%", prefixes[1] + "%",
             namespaces, "%" + old_id + "%", "%" + new_id + "%"),
        ).fetchall()
        jobs = conn.execute(
            """SELECT * FROM business.research_jobs WHERE input->'config'->>'strategy_id'=ANY(%s)
               OR result->'strategy_info'->>'id'=ANY(%s) ORDER BY id""",
            ([old_id, new_id], [old_id, new_id]),
        ).fetchall()
        pools = (conn.execute(
            "SELECT * FROM business.daily_stock_pools WHERE strategy_id=ANY(%s)", ([old_id, new_id],),
        ).fetchall() if session.db.settings.schema_version >= 7 else [])
        rules = conn.execute(
            "SELECT * FROM business.monitor_rules WHERE definition->>'strategy_id'=ANY(%s)",
            ([old_id, new_id],),
        ).fetchall()
        store = StrategyResults(session)
        states = store.states([old_id, new_id])
        if any(j["state"] in ACTIVE_TASK_STATUSES
               and (j["input"].get("config") or {}).get("strategy_id") == old_id for j in jobs):
            raise AccessError(409, "策略仍有执行中的任务, 请先停止任务后迁移")
        old_rows = [r for r in records if r["namespace"] != LIFECYCLE_NAMESPACE
                    and (old_id in r["id"] or r["id"].startswith(prefixes[0])
                         or old_id in json.dumps(r["value"], ensure_ascii=False))]
        old_jobs = [j for j in jobs if old_id in json.dumps([j["input"], j["result"]], ensure_ascii=False)]
        if (states.get(old_id, {}).get("deleted") and not old_rows and not old_jobs
                and not any(p["strategy_id"] == old_id for p in pools)
                and not any(r["definition"].get("strategy_id") == old_id for r in rules)):
            return {"already_migrated": True, "renamed_records": 0, "renamed_jobs": 0}
        if (any(r["namespace"] != LIFECYCLE_NAMESPACE and
                (new_id in r["id"] or r["id"].startswith(prefixes[1])) for r in records)
                or any(p["strategy_id"] == new_id for p in pools)
                or any(r["definition"].get("strategy_id") == new_id for r in rules)
                or any((j["input"].get("config") or {}).get("strategy_id") == new_id for j in jobs)):
            raise AccessError(409, "目标策略标识已有配置或结果, 不能覆盖")
        generation = max(int(states.get(s, {}).get("generation", 0)) for s in (old_id, new_id)) + 1
        replacements = {old_id: new_id, prefixes[0]: prefixes[1]}
        identities = {}
        days = set()
        for row in old_rows:
            if row["namespace"] != NAMESPACE:
                continue
            value = row["value"]
            summary = value["summary"]
            selected = value["result"].get("selection_snapshot") or {}
            policy = selected.get("scoring_policy") or selected.get("entry_routing") or ""
            daily = row["id"].endswith(digest({"strategy_daily_record": old_id}))
            policy_daily = bool(policy and row["id"].endswith(digest({
                "strategy_daily_record": old_id, "selection_policy": policy})))
            identity = ResultIdentity(
                new_id, summary["strategy_name"].replace(old_id, new_id), summary["strategy_version"],
                json.dumps(summary["params"]), json.dumps(_rewrite(summary["overrides"], old_id, new_id, None)),
                json.dumps(_rewrite(summary["pool"], old_id, new_id, None)), summary["asset_type"],
                summary["timeframe"], runtime_identity=summary.get("runtime_identity", ""),
                storage_generation=generation, daily_record=daily or policy_daily,
                selection_policy=policy if policy_daily else "",
            )
            identities[row["id"]] = identity
            replacements[row["id"]] = identity.record_id(summary["as_of"])
            replacements[summary["identity"]] = identity.fingerprint
            days.add((summary["as_of"], summary["asset_type"], summary["timeframe"]))
        for pool in pools:
            value = _rewrite(pool["value"], old_id, new_id, None)
            snapshot = {k: v for k, v in value.items()
                        if k not in {"id", "started_at", "computed_at", "input_generation", "offset", "limit"}}
            replacements[pool["id"]] = digest(snapshot)

        def rewrite(value):
            for before, after in sorted(replacements.items(), key=lambda p: -len(p[0])):
                value = _rewrite(value, before, after, None)
            return value

        store._set_state(conn, new_id, generation, False)
        for row in old_rows:
            namespace, item_id = row["namespace"], row["id"]
            if namespace in ("audit", "full_events"):
                continue
            value, key = rewrite(row["value"]), rewrite(item_id)
            if namespace == "strategy_pool_preferences":
                value["revision"] = int(value.get("revision", 0)) + 1
            if namespace == "strategy_sources" and isinstance(value.get("code"), str):
                value["definition"]["logic_version"] = logic_version(value["code"])
            if namespace == NAMESPACE:
                value["identifier_migration"] = {
                    "previous_identity": row["value"]["summary"]["identity"],
                    "previous_record_digest": digest(item_id),
                }
            if key != item_id and session.get(namespace, key) is not None:
                raise AccessError(409, "迁移后的记录键已存在, 不能覆盖")
            conn.execute("UPDATE business.documents SET id=%s,value=%s WHERE namespace=%s AND id=%s",
                         (key, Jsonb(value), namespace, item_id))
        for pool in pools:
            conn.execute("UPDATE business.daily_stock_pools SET id=%s,strategy_id=%s,value=%s WHERE id=%s",
                         (rewrite(pool["id"]), new_id, Jsonb(rewrite(pool["value"])), pool["id"]))
        for rule in rules:
            conn.execute("UPDATE business.monitor_rules SET definition=%s WHERE id=%s",
                         (Jsonb(rewrite(rule["definition"])), rule["id"]))
        for row in old_jobs:
            payload = rewrite(row["input"])
            payload["strategy_generation"] = generation
            metadata = rewrite(row["metadata"])
            metadata["identifier_migration"] = {
                "previous_signature": hashlib.sha256(row["signature"].encode()).hexdigest(),
                "previous_snapshot": row["metadata"].get("snapshot"),
            }
            snapshot = copy.deepcopy(payload)
            snapshot["config"] = {k: v for k, v in payload.get("config", {}).items() if k != "run_id"}
            metadata["snapshot"] = signature(snapshot)
            conn.execute(
                """UPDATE business.research_jobs SET input=%s,result=%s,events=%s,metadata=%s,
                   signature=%s WHERE id=%s""",
                (Jsonb(payload), Jsonb(rewrite(row["result"])), Jsonb(rewrite(row["events"])),
                 Jsonb(metadata), signature(payload), row["id"]),
            )
        with store.deleting_strategy(old_id):
            pass
        for day, asset, timeframe in sorted(days):
            publish(store, conn, new_id, day, asset, timeframe, cascade=False)
        session.delete("strategy_cache", "default")
    clear_memory()
    return {"renamed_records": len(identities), "renamed_jobs": len(old_jobs),
            "renamed_documents": len(old_rows), "renamed_pools": len(pools), "renamed_rules": len(rules),
            "generation": generation}


def migrate_strategy_identity(documents: TenantDocuments, old_id: str, new_id: str,
                              removed_id: str | None = None) -> dict:
    """Rename retained snapshots and retire another strategy in one tenant transaction.

    Original executions retain financial values, timestamps and provenance.
    Historical input labels are normalized; prior input digests remain in
    migration metadata so this operation never claims to be a new execution.
    """
    from psycopg.types.json import Jsonb

    ids = (old_id, new_id) if removed_id is None else (old_id, new_id, removed_id)
    if len(set(ids)) != len(ids) or not all(sid and len(sid) <= 128 for sid in ids):
        raise ValueError("strategy identities must be distinct and nonempty")
    if removed_id is None:
        return _rename_only(documents, old_id, new_id)
    with documents.transaction() as session:
        conn = session.connection
        records = conn.execute("SELECT * FROM business.documents ORDER BY namespace,id").fetchall()
        jobs = conn.execute("SELECT * FROM business.research_jobs ORDER BY id").fetchall()
        states = StrategyResults(session).states([old_id, new_id, removed_id])
        active = [job for job in jobs if job["state"] in ACTIVE_TASK_STATUSES
                  and (job["input"].get("config") or {}).get("strategy_id") in {old_id, removed_id}]
        if active:
            raise AccessError(409, "策略仍有执行中的任务, 请先停止任务后迁移")
        old_rows = [row for row in records if row["namespace"] != LIFECYCLE_NAMESPACE
                    and (row["id"] == old_id or old_id in json.dumps(row["value"], ensure_ascii=False))]
        old_jobs = [job for job in jobs if old_id in json.dumps(job["input"], ensure_ascii=False)
                    or old_id in json.dumps(job["result"], ensure_ascii=False)]
        # Repeated migration is harmless after the old identity has been fenced.
        if states.get(old_id, {}).get("deleted") and not old_rows and not old_jobs:
            return {"renamed_records": 0, "renamed_jobs": 0, "already_migrated": True}
        collision = any(
            row["namespace"] != LIFECYCLE_NAMESPACE and
            (row["id"] == new_id or (row["namespace"] == NAMESPACE and
                                    row["value"].get("summary", {}).get("strategy_id") == new_id))
            for row in records
        ) or any((job["input"].get("config") or {}).get("strategy_id") == new_id for job in jobs)
        if collision:
            raise AccessError(409, "目标策略标识已有配置或结果, 不能覆盖")
        generation = max(int(states.get(new_id, {}).get("generation", 0)),
                         int(states.get(old_id, {}).get("generation", 0))) + 1
        store = StrategyResults(session)
        store._set_state(conn, new_id, generation, False)
        renamed_records = 0
        for row in records:
            namespace, item_id, value = row["namespace"], row["id"], row["value"]
            if namespace == LIFECYCLE_NAMESPACE:
                continue
            if namespace == NAMESPACE and value["summary"]["strategy_id"] == removed_id:
                session.delete(namespace, item_id)
                continue
            new_value = _rewrite(value, old_id, new_id, removed_id)
            if item_id == removed_id or new_value is _DROP:
                session.delete(namespace, item_id)
                continue
            new_key = item_id.replace(old_id, new_id)
            if namespace == NAMESPACE and value["summary"]["strategy_id"] == old_id:
                summary = new_value["summary"]
                identity = ResultIdentity(
                    new_id, summary["strategy_name"], summary["strategy_version"],
                    json.dumps(summary["params"], ensure_ascii=False), json.dumps(summary["overrides"], ensure_ascii=False),
                    json.dumps(summary["pool"], ensure_ascii=False), summary["asset_type"], summary["timeframe"],
                    runtime_identity=summary.get("runtime_identity", ""), storage_generation=generation,
                    daily_record=item_id.endswith(digest({"strategy_daily_record": old_id})),
                )
                new_key = identity.record_id(summary["as_of"])
                new_value["record_id"] = new_key
                summary.update(record_id=new_key, identity=identity.fingerprint)
                new_value["result"]["record_id"] = new_key
                renamed_records += 1
            if new_key != item_id or new_value != value:
                if new_key != item_id and session.get(namespace, new_key) is not None:
                    raise AccessError(409, "迁移后的记录键已存在, 不能覆盖")
                conn.execute(
                    "UPDATE business.documents SET id=%s,value=%s WHERE namespace=%s AND id=%s",
                    (new_key, Jsonb(new_value), namespace, item_id),
                )
        removed_jobs = sum((job["input"].get("config") or {}).get("strategy_id") == removed_id for job in jobs)
        ResearchJobs(session).delete_strategy(removed_id)
        renamed_jobs = 0
        for row in jobs:
            if (row["input"].get("config") or {}).get("strategy_id") == removed_id:
                continue
            new_input = _rewrite(row["input"], old_id, new_id, removed_id)
            new_result = _rewrite(row["result"], old_id, new_id, removed_id)
            new_events = _rewrite(row["events"], old_id, new_id, removed_id)
            metadata = _rewrite(row["metadata"], old_id, new_id, removed_id)
            if new_input == row["input"] and new_result == row["result"] and new_events == row["events"]:
                continue
            if (new_input.get("config") or {}).get("strategy_id") == new_id:
                new_input["strategy_generation"] = generation
            snapshot = copy.deepcopy(new_input)
            snapshot["config"] = {k: v for k, v in new_input.get("config", {}).items() if k != "run_id"}
            metadata["identifier_migration"] = {
                "at": datetime.now(UTC).isoformat(),
                "previous_signature": hashlib.sha256(row["signature"].encode()).hexdigest(),
                "previous_snapshot": row["metadata"].get("snapshot"),
            }
            metadata["snapshot"] = signature(snapshot)
            conn.execute(
                """UPDATE business.research_jobs SET input=%s,result=%s,events=%s,metadata=%s,
                   signature=%s,updated_at=now() WHERE id=%s""",
                (Jsonb(new_input), Jsonb(new_result), Jsonb(new_events), Jsonb(metadata),
                 signature(new_input), row["id"]),
            )
            renamed_jobs += 1
        for sid in (old_id, removed_id):
            with store.deleting_strategy(sid):
                pass
        session.delete("strategy_cache", "default")
    clear_memory()
    return {"renamed_records": renamed_records, "renamed_jobs": renamed_jobs, "removed_jobs": removed_jobs}
