"""Atomic, indexed projections of saved signal days; GET never computes a strategy."""
from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import threading
from collections import OrderedDict
from datetime import date, timedelta

from app.strategy.version import digest

POINTER_NAMESPACE = "strategy_day_pointer"
PAYLOAD_NAMESPACE = "strategy_day_payload"
MEMORY_BYTES = 32 * 1024 * 1024
_memory: OrderedDict = OrderedDict()
_memory_size = 0
_lock = threading.Lock()


def clear_memory():
    global _memory_size
    with _lock:
        _memory.clear()
        _memory_size = 0


def _key(strategy_id, day, asset_type, timeframe):
    if asset_type != "stock" or timeframe != "1m":
        raise ValueError("历史信号视图只支持stock/1m")
    return f"{digest(strategy_id)[:16]}:{date.fromisoformat(str(day))}:{asset_type}:{timeframe}"


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_day_views(
        namespace TEXT NOT NULL,id TEXT NOT NULL,value TEXT NOT NULL,
        PRIMARY KEY(namespace,id))""")


def _get(store, conn, namespace, key):
    if store.tenant:
        row = conn.execute(
            "SELECT value FROM business.documents WHERE namespace=%s AND id=%s",
            (namespace, key),
        ).fetchone()
        return row["value"] if row else None
    row = conn.execute(
        "SELECT value FROM strategy_day_views WHERE namespace=? AND id=?", (namespace, key),
    ).fetchone()
    return json.loads(row["value"]) if row else None


def _put(store, conn, namespace, key, value):
    if store.tenant:
        from psycopg.types.json import Jsonb
        conn.execute(
            """INSERT INTO business.documents(tenant_id,namespace,id,value) VALUES(%s,%s,%s,%s)
               ON CONFLICT(tenant_id,namespace,id) DO UPDATE SET value=excluded.value,
               revision=business.documents.revision+1,updated_at=now()""",
            (store.storage.context.tenant_id, namespace, key, Jsonb(value)),
        )
    else:
        from app.services.strategy_results import _encode
        conn.execute(
            """INSERT INTO strategy_day_views VALUES(?,?,?)
               ON CONFLICT(namespace,id) DO UPDATE SET value=excluded.value""",
            (namespace, key, _encode(value)),
        )


def remove(store, conn, strategy_id, day=None):
    prefix = digest(strategy_id)[:16] + ":" + (str(day) + ":" if day else "")
    if store.tenant:
        conn.execute(
            "DELETE FROM business.documents WHERE namespace=ANY(%s) AND id LIKE %s",
            ([POINTER_NAMESPACE, PAYLOAD_NAMESPACE], prefix + "%"),
        )
    else:
        conn.execute("DELETE FROM strategy_day_views WHERE id LIKE ?", (prefix + "%",))


def publish(store, conn, strategy_id, day, asset_type="stock", timeframe="1m"):
    """Called only in the authority's write transaction, including delete/backfill."""
    from app.services.strategy_results import NAMESPACE, ResultIdentity, _encode

    key = _key(strategy_id, day, asset_type, timeframe)
    prefix = digest(strategy_id)[:16] + ":"
    lower = prefix + str(day) + ":"
    upper = prefix + str(date.fromisoformat(str(day)) + timedelta(days=1)) + ":"
    if store.tenant:
        rows = conn.execute(
            """SELECT value FROM business.documents WHERE namespace=%s AND id>=%s AND id<%s
               AND value->'result'->>'section_mode'='signals'""", (NAMESPACE, lower, upper),
        ).fetchall()
        values = [row["value"] for row in rows]
    else:
        rows = conn.execute(
            """SELECT value FROM strategy_results WHERE id>=? AND id<?
               AND json_extract(value,'$.result.section_mode')='signals'""", (lower, upper),
        ).fetchall()
        values = [json.loads(row["value"]) for row in rows]
    values = [v for v in values if v["summary"]["strategy_id"] == strategy_id
              and v["summary"]["asset_type"] == asset_type and v["summary"]["timeframe"] == timeframe]
    if not values:
        remove(store, conn, strategy_id, day)
        return
    chosen = max(values, key=lambda v: (
        not v["summary"].get("selection_only"), v["summary"].get("started_at") or 0,
        v["summary"].get("computed_at") or 0, v["record_id"],
    ))
    result = copy.deepcopy(chosen["result"])
    frozen_id = ResultIdentity(strategy_id, "", "", "{}", "{}", "null",
                               asset_type, timeframe, daily_record=True).record_id(str(day))
    frozen = next((v for v in values if v["record_id"] == frozen_id), {})
    selected = frozen.get("result", {}).get("selection_snapshot")
    if not result.get("selection_snapshot") and selected is not None:
        result["selection_snapshot"] = selected
        result["sections"]["select"] = selected["rows"]
        result["section_errors"] = {k: v for k, v in (result.get("section_errors") or {}).items()
                                    if k != "select"}
    payload = {
        "result": {**result, "strategy": strategy_id, "source": "saved",
                   "record_id": chosen["record_id"],
                   "strategy_version": chosen["summary"]["strategy_version"]},
        "summary": chosen["summary"], "today_ever_rows": chosen.get("ever_rows"),
        "strategy_ids_by_symbol": {}, "updated_at": chosen["summary"]["computed_at"],
        "view_schema": 1,
    }
    body = gzip.compress(_encode(payload).encode("utf-8"), mtime=0)
    token = hashlib.sha256(body).hexdigest()
    if (_get(store, conn, POINTER_NAMESPACE, key) or {}).get("token") == token:
        return
    _put(store, conn, PAYLOAD_NAMESPACE, key, {"body": base64.b64encode(body).decode(), "token": token})
    _put(store, conn, POINTER_NAMESPACE, key, {"payload_id": key, "token": token})


def read(store, strategy_id, day, asset_type="stock", timeframe="1m"):
    global _memory_size
    key = _key(strategy_id, day, asset_type, timeframe)
    transaction = store.storage._transaction if store.tenant else store._local
    with transaction() as conn:
        if conn is None:
            return None
        if not store.tenant and not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='strategy_day_views'",
        ).fetchone():
            return None
        pointer = _get(store, conn, POINTER_NAMESPACE, key)
        if pointer is None:
            return None
        cache_key = (store.scope, key, pointer["token"])
        if store.cacheable:
            with _lock:
                body = _memory.get(cache_key)
                if body is not None:
                    _memory.move_to_end(cache_key)
                    return pointer["token"], body
        payload = _get(store, conn, PAYLOAD_NAMESPACE, pointer["payload_id"])
        if payload is None or payload["token"] != pointer["token"]:
            raise RuntimeError("历史视图正文与指针不一致")
        body = base64.b64decode(payload["body"], validate=True)
    if store.cacheable and len(body) <= MEMORY_BYTES:
        with _lock:
            prior = _memory.pop(cache_key, None)
            if prior:
                _memory_size -= len(prior)
            while _memory and _memory_size + len(body) > MEMORY_BYTES:
                _, prior = _memory.popitem(last=False)
                _memory_size -= len(prior)
            _memory[cache_key] = body
            _memory_size += len(body)
    return pointer["token"], body
