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
        if value.get("strategy_id") == removed_id or value.get("id") == removed_id:
            return _DROP
        result = {}
        for key, item in value.items():
            if removed_id in key:
                continue
            item = _rewrite(item, old_id, new_id, removed_id)
            if item is not _DROP:
                result[key.replace(old_id, new_id)] = item
        return result
    return value


def migrate_strategy_identity(documents: TenantDocuments, old_id: str, new_id: str, removed_id: str) -> dict:
    """Rename retained snapshots and retire another strategy in one tenant transaction.

    Original executions retain financial values, timestamps and provenance.
    Historical input labels are normalized; prior input digests remain in
    migration metadata so this operation never claims to be a new execution.
    """
    from psycopg.types.json import Jsonb

    if len({old_id, new_id, removed_id}) != 3 or not all((sid and len(sid) <= 128) for sid in (old_id, new_id, removed_id)):
        raise ValueError("strategy identities must be distinct and nonempty")
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
