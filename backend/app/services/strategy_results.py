"""Durable strategy snapshots. SQL owns history; bounded memory accelerates detail reads."""
from __future__ import annotations

import copy
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from app.multitenancy.documents import DocumentSession, TenantDocuments, json_value
from app.strategy.eligibility import ENTRY_POLICY_VERSION
from app.strategy.version import DISPLAY_FIELDS, digest, logic_version, runtime_identity

logger = logging.getLogger(__name__)
NAMESPACE = "strategy_result_history"
MEMORY_BYTES = 32 * 1024 * 1024
_memory: OrderedDict = OrderedDict()
_memory_size = 0
_memory_lock = threading.Lock()


def clear_memory() -> None:
    global _memory_size
    with _memory_lock:
        _memory.clear()
        _memory_size = 0


def _remember(key, value):
    global _memory_size
    encoded = _encode(value).encode("utf-8")
    size = len(encoded)
    with _memory_lock:
        previous = _memory.pop(key, None)
        if previous:
            _memory_size -= previous[1]
        if size > MEMORY_BYTES:
            return
        while _memory and _memory_size + size > MEMORY_BYTES:
            _, (_, old_size) = _memory.popitem(last=False)
            _memory_size -= old_size
        _memory[key] = (encoded, size)
        _memory_size += size


def _encode(value) -> str:
    def default(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError("unsupported JSON value")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=default,
                      separators=(",", ":"))


@dataclass(frozen=True)
class ResultIdentity:
    strategy_id: str
    strategy_name: str
    strategy_version: str
    params_json: str
    overrides_json: str
    pool_json: str
    asset_type: str
    timeframe: str
    runtime_identity: str = ""

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))

    def record_id(self, as_of: str) -> str:
        return f"{digest(self.strategy_id)[:16]}:{date.fromisoformat(str(as_of)).isoformat()}:{self.fingerprint}"

    def snapshot(self) -> dict:
        return {
            "strategy_id": self.strategy_id, "strategy_name": self.strategy_name,
            "strategy_version": self.strategy_version, "params": json.loads(self.params_json),
            "overrides": json.loads(self.overrides_json), "pool": json.loads(self.pool_json),
            "asset_type": self.asset_type, "timeframe": self.timeframe,
            "identity": self.fingerprint,
            "runtime_identity": self.runtime_identity,
        }


def execution_snapshot(engine):
    """Keep the definition/configuration used by an in-flight run stable during reload."""
    if not hasattr(engine, "_strategies"):
        return engine
    snapshot = copy.copy(engine)
    if hasattr(engine, "_strategies"):
        snapshot._strategies = dict(engine._strategies)
    loader = getattr(engine, "_override_loader", None)
    if callable(loader):
        overrides = {sid: loader(sid) for sid in snapshot._strategies}
        snapshot._override_loader = lambda sid: copy.deepcopy(overrides.get(sid, {}))
    if getattr(engine, "remote_context", False):
        snapshot._task_snapshot = engine.task("strategy")
    return snapshot


def input_generation(repo, engine, asset_type):
    getter = getattr(repo, "get_matrix_data_generation", None)
    return {"market": getter(asset_type) if callable(getter) else None,
            "public_release_id": getattr(engine, "public_release_id", None)}


def _dependencies(storage):
    from app.factors import store as factor_store
    from app.strategy import custom_signals

    def semantic(value):
        if isinstance(value, dict):
            return {k: semantic(v) for k, v in value.items()
                    if k not in DISPLAY_FIELDS | {"_storage_revision", "created_at", "updated_at"}}
        if isinstance(value, list):
            return [semantic(v) for v in value]
        return value

    return {"factors": semantic(factor_store.load_all(storage)),
            "signals": semantic(custom_signals.load_all(storage))}


def capture_identities(engine, storage, strategy_ids, *, params_map=None, overrides_map=None,
                       asset_type="stock", timeframe="1d"):
    dependencies = _dependencies(storage)
    return {
        sid: capture_identity(
            engine, storage, sid, params=(params_map or {}).get(sid),
            overrides=(overrides_map or {}).get(sid), asset_type=asset_type,
            timeframe=(engine.get(sid).meta.get("timeframes") or ["1d"])[0] if timeframe == "all" else timeframe,
            dependencies=dependencies,
        ) for sid in strategy_ids
    }


def capture_identity(engine, storage, strategy_id, *, params=None, overrides=None,
                     pool=None, asset_type="stock", timeframe="1d", dependencies=None) -> ResultIdentity:
    """Capture before execution, using the loaded definition and resolved defaults."""
    strategy = engine.get(strategy_id)
    from app.strategy.engine import StrategyEngine
    resolved = StrategyEngine.resolve_params(strategy, params, overrides)
    configuration = {key: value for key, value in (overrides or {}).items()
                     if key not in DISPLAY_FIELDS and key != "params"}

    def version(sid):
        definition = engine.get(sid)
        captured = getattr(definition, "logic_version", "")
        if not captured:
            # Old tenant definitions have code but predate the version field.
            source = getattr(engine, "source_code", None)
            captured = logic_version(source(sid)) if callable(source) else digest({
                "meta": {k: v for k, v in definition.meta.items() if k not in DISPLAY_FIELDS},
                "basic_filter": getattr(definition, "basic_filter", {}),
            })
        children = []
        if getattr(definition, "composite", None):
            for child in definition.composite.children:
                override_loader = getattr(engine, "_override_loader", None)
                child_override = override_loader(child.strategy_id) if callable(override_loader) else {}
                children.append({"id": child.strategy_id, "version": version(child.strategy_id),
                                 "overrides": child_override})
        return {"logic": captured, "children": children}

    # Private formula changes affect computed columns even if the strategy source stays unchanged.
    if dependencies is None:
        dependencies = _dependencies(storage)
    return ResultIdentity(
        strategy_id=strategy_id,
        strategy_name=str((overrides or {}).get("name") or strategy.meta.get("name") or strategy_id),
        strategy_version=digest({"strategy": version(strategy_id),
                                 "entry_policy": ENTRY_POLICY_VERSION, "dependencies": dependencies}),
        params_json=json.dumps(resolved, sort_keys=True, ensure_ascii=False, allow_nan=False),
        overrides_json=json.dumps(configuration, sort_keys=True, ensure_ascii=False, allow_nan=False),
        pool_json=json.dumps(sorted(set(pool)) if pool is not None else None),
        asset_type=asset_type, timeframe=timeframe,
        runtime_identity=runtime_identity(strategy, overrides or {}),
    )


class StrategyResults:
    def __init__(self, storage):
        self.storage = storage
        self.tenant = isinstance(storage, TenantDocuments)
        self.cacheable = not isinstance(storage, DocumentSession)
        self.path = None if self.tenant else Path(storage) / "user_data" / "strategy_results.sqlite3"
        self.scope = (("tenant", id(storage.db), storage.context.tenant_id) if self.tenant
                      else ("local", str(self.path.resolve())))

    @contextmanager
    def _local(self, *, write=False):
        if not write and not self.path.exists():
            yield None
            return
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, timeout=30)
        else:
            conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=rw", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("""CREATE TABLE IF NOT EXISTS strategy_results(
                    id TEXT PRIMARY KEY, summary TEXT NOT NULL, value TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1)""")
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("BEGIN")
            yield conn
            if write:
                conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save(self, identity: ResultIdentity, result: dict, *, started_at: int,
             input_generation=None) -> dict:
        as_of = date.fromisoformat(str(result["as_of"])).isoformat()
        record_id = identity.record_id(as_of)

        def merged(old):
            if old and old["summary"]["started_at"] > started_at:
                return old
            current = json_value(result)
            current["computed_at"] = int(time.time() * 1000)
            ever = {**(old["ever_rows"] if old else {}),
                    **{str(row["symbol"]): row for row in current.get("rows", [])
                       if row.get("symbol")}}
            summary = {
                **identity.snapshot(), "record_id": record_id, "as_of": as_of,
                "total": int(current.get("total") or 0), "ever_count": len(ever),
                "started_at": started_at, "computed_at": current["computed_at"],
                "input_generation": input_generation,
            }
            return json_value({"record_id": record_id, "summary": summary,
                               "result": current, "ever_rows": ever})

        if self.tenant:
            from psycopg.types.json import Jsonb
            with self.storage._transaction(write=True) as conn:
                row = conn.execute(
                    "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
                    (NAMESPACE, record_id),
                ).fetchone()
                value = merged(row["value"] if row else None)
                conn.execute(
                    """INSERT INTO business.documents(tenant_id,namespace,id,value)
                       VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET
                       value=excluded.value,revision=business.documents.revision+1,updated_at=now()""",
                    (self.storage.context.tenant_id, NAMESPACE, record_id, Jsonb(value)),
                )
        else:
            with self._local(write=True) as conn:
                row = conn.execute("SELECT value FROM strategy_results WHERE id=?", (record_id,)).fetchone()
                value = merged(json.loads(row["value"]) if row else None)
                conn.execute(
                    """INSERT INTO strategy_results(id,summary,value) VALUES(?,?,?)
                       ON CONFLICT(id) DO UPDATE SET summary=excluded.summary,value=excluded.value,
                       revision=strategy_results.revision+1""",
                    (record_id, _encode(value["summary"]), _encode(value)),
                )
        logger.info("strategy result saved: asset=%s timeframe=%s date=%s",
                    identity.asset_type, identity.timeframe, as_of)
        return copy.deepcopy(value)

    def read(self, identity: ResultIdentity, as_of: str) -> dict | None:
        return self.detail(identity.strategy_id, identity.record_id(as_of))

    def detail(self, strategy_id: str, record_id: str) -> dict | None:
        prefix = digest(strategy_id)[:16] + ":"
        if not record_id.startswith(prefix) or len(record_id) != 92:
            return None
        if self.tenant:
            with self.storage._transaction() as conn:
                row = conn.execute(
                    "SELECT revision FROM business.documents WHERE namespace=%s AND id=%s",
                    (NAMESPACE, record_id),
                ).fetchone()
                if not row:
                    return None
                key = (self.scope, record_id, row["revision"])
                cached = self._hot(key) if self.cacheable else None
                if cached is not None:
                    return cached
                row = conn.execute(
                    "SELECT value,revision FROM business.documents WHERE namespace=%s AND id=%s",
                    (NAMESPACE, record_id),
                ).fetchone()
                if row is None:
                    return None
                value = row["value"]
                key = (self.scope, record_id, row["revision"])
        else:
            with self._local() as conn:
                if conn is None:
                    return None
                row = conn.execute("SELECT revision FROM strategy_results WHERE id=?", (record_id,)).fetchone()
                if not row:
                    return None
                key = (self.scope, record_id, row["revision"])
                cached = self._hot(key)
                if cached is not None:
                    return cached
                value = json.loads(conn.execute(
                    "SELECT value FROM strategy_results WHERE id=?", (record_id,),
                ).fetchone()["value"])
        if value["summary"]["strategy_id"] != strategy_id:
            return None
        if self.cacheable:
            _remember(key, value)
        return copy.deepcopy(value)

    @staticmethod
    def _hot(key):
        with _memory_lock:
            entry = _memory.get(key)
            if entry is not None:
                _memory.move_to_end(key)
                return json.loads(entry[0])
        return None

    def summaries(self, identities: list[ResultIdentity], as_of: str) -> dict:
        ids = [identity.record_id(as_of) for identity in identities]
        if not ids:
            return {}
        if self.tenant:
            with self.storage._transaction() as conn:
                rows = conn.execute(
                    "SELECT value->'summary' AS summary FROM business.documents WHERE namespace=%s AND id=ANY(%s)",
                    (NAMESPACE, ids),
                ).fetchall()
            values = [row["summary"] for row in rows]
        else:
            with self._local() as conn:
                if conn is None:
                    return {}
                rows = conn.execute(
                    f"SELECT summary FROM strategy_results WHERE id IN ({','.join('?' for _ in ids)})", ids,
                ).fetchall()
            values = [json.loads(row["summary"]) for row in rows]
        return {value["strategy_id"]: value for value in values}

    def list_history(self, strategy_id: str, *, limit=30, cursor=None) -> dict:
        if not 1 <= limit <= 100:
            raise ValueError("历史分页数量必须在1到100之间")
        prefix = digest(strategy_id)[:16] + ":"
        if cursor and (not cursor.startswith(prefix) or len(cursor) != 92):
            raise ValueError("历史分页游标无效")
        # ASCII "~" is not an upper bound in locale-aware PostgreSQL collations.
        # The next fixed-width hex strategy prefix bounds the same PK index range.
        number = int(prefix[:16], 16)
        upper = cursor or (f"{number + 1:016x}" if number < 2**64 - 1 else "g")
        if self.tenant:
            with self.storage._transaction() as conn:
                rows = conn.execute(
                    """SELECT id,value->'summary' AS summary FROM business.documents
                       WHERE namespace=%s AND id>=%s AND id<%s ORDER BY id DESC LIMIT %s""",
                    (NAMESPACE, prefix, upper, limit + 1),
                ).fetchall()
            values = [row["summary"] for row in rows]
        else:
            with self._local() as conn:
                if conn is None:
                    return {"records": [], "next_cursor": None}
                rows = conn.execute(
                    """SELECT id,summary FROM strategy_results
                       WHERE id>=? AND id<? ORDER BY id DESC LIMIT ?""", (prefix, upper, limit + 1),
                ).fetchall()
            values = [json.loads(row["summary"]) for row in rows]
        return {"records": values[:limit],
                "next_cursor": values[limit - 1]["record_id"] if len(values) > limit else None}

    def memberships(self, identities, as_of, symbols):
        ids = [identity.record_id(as_of) for identity in identities]
        if not ids or not symbols:
            return {}
        if self.tenant:
            with self.storage._transaction() as conn:
                rows = conn.execute(
                    """SELECT d.value->'summary'->>'strategy_id' AS sid,r->>'symbol' AS symbol
                       FROM business.documents d,
                       LATERAL jsonb_array_elements(d.value->'result'->'rows') r
                       WHERE d.namespace=%s AND d.id=ANY(%s) AND r->>'symbol'=ANY(%s)""",
                    (NAMESPACE, ids, sorted(symbols)),
                ).fetchall()
        else:
            with self._local() as conn:
                if conn is None:
                    return {}
                rows = conn.execute(
                    f"""SELECT json_extract(d.summary,'$.strategy_id') AS sid,
                        json_extract(r.value,'$.symbol') AS symbol
                        FROM strategy_results d,json_each(d.value,'$.result.rows') r
                        WHERE d.id IN ({','.join('?' for _ in ids)})
                        AND json_extract(r.value,'$.symbol') IN ({','.join('?' for _ in symbols)})""",
                    [*ids, *sorted(symbols)],
                ).fetchall()
        members = {symbol: set() for symbol in symbols}
        for row in rows:
            members[row["symbol"]].add(row["sid"])
        return {symbol: sorted(strategies) for symbol, strategies in members.items()}


def persist_result(storage, identity, result, *, started_at, input_generation=None):
    """Storage failures must reach callers; a successful calculation is not a successful save."""
    try:
        return StrategyResults(storage).save(identity, result, started_at=started_at,
                                             input_generation=input_generation)
    except Exception:
        logger.error("strategy result persistence failed")
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="策略结果保存失败, 上一份成功记录已保留, 请重试") from None
