"""Durable, tenant-isolated daily scope snapshots in the result transaction."""
import json
import threading
import time

from app.cell.context import AccessError
from app.multitenancy.documents import json_value
from app.services.strategy_results import StrategyResults, _encode
from app.strategy.version import digest

_scheduled_lock = threading.Lock()


class DailyStockPools:
    def __init__(self, storage):
        self.results = StrategyResults(storage)

    def job(self, key, value=None):
        if self.results.tenant:
            storage = self.results.storage
            return (storage.get("daily_stock_pool_jobs", key, {}) if value is None
                    else storage.put("daily_stock_pool_jobs", key, value))
        with self.results._local(write=value is not None) as conn:
            if conn is None:
                return {}
            if value is not None:
                conn.execute("CREATE TABLE IF NOT EXISTS daily_stock_pool_jobs(key TEXT PRIMARY KEY,value TEXT)")
                conn.execute("INSERT INTO daily_stock_pool_jobs VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _encode(value)))
                return value
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='daily_stock_pool_jobs'").fetchone():
                return {}
            row = conn.execute("SELECT value FROM daily_stock_pool_jobs WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else {}

    def save(self, identity, pool, *, started_at, input_generation=None):
        transaction = (self.results.storage._transaction if self.results.tenant else self.results._local)
        with transaction(write=True) as conn:
            self.results._check_generation(conn, identity.strategy_id, identity.storage_generation)
            return self.save_in_transaction(conn, identity, pool, started_at=started_at,
                                            input_generation=input_generation)

    def save_in_transaction(self, conn, identity, pool, *, started_at, input_generation=None):
        from datetime import date
        day = date.fromisoformat(str(pool["as_of"])).isoformat()
        if pool["strategy_id"] != identity.strategy_id:
            raise ValueError("股票池策略身份不匹配")
        symbols = sorted(set(pool["symbols"]))
        if pool["total"] != len(symbols):
            raise ValueError("股票池数量不匹配")
        snapshot = {**pool, "symbols": symbols}
        record_id = digest(snapshot)
        value = json_value({**snapshot, "id": record_id, "started_at": started_at,
                            "computed_at": int(time.time() * 1000), "input_generation": input_generation})
        if self.results.tenant:
            from psycopg.types.json import Jsonb
            # The existing writable tenant transaction serializes competing
            # writes. Check run order before replacing the day's only pool.
            latest = conn.execute(
                """SELECT started_at,value FROM business.daily_stock_pools
                   WHERE strategy_id=%s AND day=%s ORDER BY started_at DESC,id DESC LIMIT 1""",
                (identity.strategy_id, day),
            ).fetchone()
            if latest and latest["started_at"] > started_at:
                return latest["value"]
            conn.execute(
                """INSERT INTO business.daily_stock_pools
                   (tenant_id,id,strategy_id,day,started_at,value) VALUES(%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(tenant_id,id) DO UPDATE SET
                     started_at=greatest(business.daily_stock_pools.started_at,excluded.started_at),
                     value=CASE WHEN excluded.started_at>business.daily_stock_pools.started_at
                       THEN business.daily_stock_pools.value || jsonb_build_object(
                         'started_at',excluded.started_at,'input_generation',excluded.value->'input_generation')
                       ELSE business.daily_stock_pools.value END""",
                (self.results.storage.context.tenant_id, record_id, identity.strategy_id, day,
                 started_at, Jsonb(value)),
            )
            conn.execute(
                "DELETE FROM business.daily_stock_pools WHERE strategy_id=%s AND day=%s AND id<>%s",
                (identity.strategy_id, day, record_id),
            )
            value = conn.execute("SELECT value FROM business.daily_stock_pools WHERE id=%s",
                                 (record_id,)).fetchone()["value"]
        else:
            latest = conn.execute(
                """SELECT started_at,value FROM daily_stock_pools
                   WHERE strategy_id=? AND day=? ORDER BY started_at DESC,id DESC LIMIT 1""",
                (identity.strategy_id, day),
            ).fetchone()
            if latest and latest["started_at"] > started_at:
                return json.loads(latest["value"])
            conn.execute(
                """INSERT INTO daily_stock_pools(id,strategy_id,day,started_at,value) VALUES(?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     started_at=max(daily_stock_pools.started_at,excluded.started_at),
                     value=CASE WHEN excluded.started_at>daily_stock_pools.started_at
                       THEN json_set(daily_stock_pools.value,'$.started_at',excluded.started_at,
                         '$.input_generation',json(json_extract(excluded.value,'$.input_generation')))
                       ELSE daily_stock_pools.value END""",
                (record_id, identity.strategy_id, day, started_at, _encode(value)),
            )
            conn.execute("DELETE FROM daily_stock_pools WHERE strategy_id=? AND day=? AND id<>?",
                         (identity.strategy_id, day, record_id))
            value = json.loads(conn.execute("SELECT value FROM daily_stock_pools WHERE id=?",
                                            (record_id,)).fetchone()["value"])
        return value

    def read(self, strategy_id, day, *, record_id=None, limit=500, offset=0):
        from datetime import date
        day = date.fromisoformat(str(day)).isoformat()
        transaction = (self.results.storage._transaction if self.results.tenant else self.results._local)
        with transaction() as conn:
            if conn is None:
                return None
            if self.results.tenant:
                row = conn.execute(
                    """SELECT value FROM business.daily_stock_pools
                       WHERE strategy_id=%s AND day=%s AND (%s::text IS NULL OR id=%s)
                       ORDER BY started_at DESC,id DESC LIMIT 1""",
                    (strategy_id, day, record_id, record_id),
                ).fetchone()
                value = row["value"] if row else None
            else:
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_stock_pools'",
                ).fetchone():
                    return None
                row = conn.execute(
                    """SELECT value FROM daily_stock_pools WHERE strategy_id=? AND day=?
                       AND (? IS NULL OR id=?) ORDER BY started_at DESC,id DESC LIMIT 1""",
                    (strategy_id, day, record_id, record_id),
                ).fetchone()
                value = json.loads(row["value"]) if row else None
        if value is None:
            return None
        return {**value, "symbols": value["symbols"][offset:offset + limit], "offset": offset, "limit": limit}


def generate_local(engine, service, storage, as_of, *, slot=None):
    from app.services.daily_pool_schedule import batch_pool
    from app.services.strategy_results import capture_identities, execution_snapshot
    from app.strategy.config import list_overrides
    from app.strategy.daily_pool import build_pool

    engine = execution_snapshot(engine, storage)
    overrides = list_overrides(storage)
    ids = [sid for sid, strategy in engine._strategies.items()
           if not strategy.meta.get("research_only") and "stock" in strategy.meta.get("asset_types", ["stock"])]
    identities = capture_identities(engine, storage, ids, overrides_map=overrides, timeframe="all")
    store = DailyStockPools(storage)
    marker = digest({sid: identity.fingerprint for sid, identity in identities.items()})
    job_key = f"{as_of}:{slot}"
    previous = store.job(job_key) if slot else {}
    completed = set(previous.get("completed", [])) if previous.get("signature") == marker else set()
    if slot and previous.get("state") == "succeeded":
        return {"saved": [], "errors": {}}
    pending = [sid for sid in ids if sid not in completed]
    if slot:
        store.job(job_key, {"signature": marker, "state": "running", "completed": sorted(completed),
                           "started_at": int(time.time() * 1000),
                           "attempts": previous.get("attempts", 0) + 1})
    saved, errors = [], {}
    started_at = time.time_ns()
    try:
        context = (service.build_daily_pool_context(as_of, premarket=True) if slot
                   else service.build_daily_pool_context(as_of))
    except Exception:
        if slot:
            store.job(job_key, {**store.job(job_key), "state": "failed", "errors": {
                sid: "当日股票池缺少所需参考数据" for sid in pending}})
        raise
    for sid in pending:
        try:
            pool = build_pool(engine, sid, context, overrides=overrides.get(sid), premarket=bool(slot))
            if slot:
                pool = batch_pool(pool, as_of, slot)
            store.save(identities[sid], pool, started_at=started_at)
            saved.append(sid)
        except (ValueError, AccessError) as exc:
            errors[sid] = str(exc)
    if slot:
        store.job(job_key, {**store.job(job_key), "completed": sorted(completed | set(saved)),
                           "state": "failed" if errors else "succeeded", "errors": errors,
                           "finished_at": int(time.time() * 1000)})
    return {"saved": saved, "errors": errors}


def generate_scheduled_local(engine, service, storage, *, now=None):
    from app.market_time import as_cn_datetime, cn_now
    from app.services.daily_pool_schedule import due_slot
    now = as_cn_datetime(now or cn_now())
    slot = due_slot(now)
    if slot is None or not _scheduled_lock.acquire(blocking=False):
        return {"saved": [], "errors": {}}
    try:
        store = DailyStockPools(storage)
        if slot == "09:00":
            earlier = f"{now.date()}:08:00"
            previous = store.job(earlier)
            if previous.get("state") not in {"succeeded", "interrupted"}:
                store.job(earlier, {**previous, "state": "interrupted",
                                   "reason": "已错过08:00批次, 生成最近09:00批次"})
        return generate_local(engine, service, storage, now.date(), slot=slot)
    finally:
        _scheduled_lock.release()
