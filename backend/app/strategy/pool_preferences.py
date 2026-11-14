"""Account-owned screener selections; monitoring and strategy definitions remain separate."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app.cell.context import AccessError
from app.multitenancy.documents import TenantDocuments
from app.services.fs_utils import atomic_write_text

_LOCK = threading.Lock()
_NAMESPACE = "strategy_pool_preferences"


def _file(storage: Path) -> Path:
    return storage / "user_data" / "strategy_pool.json"


def _read(storage):
    if isinstance(storage, TenantDocuments):
        return storage.get(_NAMESPACE, storage.context.user_id)
    path = _file(storage)
    if not path.exists():
        return None
    # Corrupt or unreadable preferences must not become an empty writable pool.
    return json.loads(path.read_text(encoding="utf-8"))


def _defaults(storage, valid_ids):
    from app.strategy.monitor_rules import load_all

    return list(dict.fromkeys(
        rule["strategy_id"] for rule in load_all(storage)
        if rule.get("type") == "strategy" and rule.get("enabled")
        and rule.get("strategy_id") in valid_ids
    ))


def _view(value, storage, valid_ids):
    return {"ids": list(value["ids"]) if value is not None else _defaults(storage, valid_ids),
            "revision": value["revision"] if value is not None else 0}


def load(storage, valid_ids):
    return _view(_read(storage), storage, valid_ids)


def _updated(current, storage, valid_ids, ids, revision, migrate):
    view = _view(current, storage, valid_ids)
    if migrate:
        if current is not None and not current.get("legacy_import", False):
            return current
        ids = list(dict.fromkeys([*view["ids"], *(sid for sid in ids if sid in valid_ids)]))
        if ids == view["ids"]:
            return current
    else:
        if revision != view["revision"]:
            raise AccessError(409, "策略池已在其他页面更新, 请重新打开后再保存")
        if any(sid not in valid_ids for sid in ids):
            raise AccessError(400, "策略池包含不可用的策略, 请刷新后重试")
        if current is not None and ids == view["ids"] and not current.get("legacy_import"):
            return current
    return {"ids": ids, "revision": view["revision"] + 1, "legacy_import": migrate}


def save(storage, valid_ids, *, ids, revision, migrate=False):
    if len(ids) != len(set(ids)):
        raise AccessError(400, "策略池不能包含重复策略")
    if isinstance(storage, TenantDocuments):
        from app.multitenancy.events import append_event

        with storage.transaction(write=True) as session:
            current = _read(session)
            value = _updated(current, session, valid_ids, ids, revision, migrate)
            if value != current:
                session.put(_NAMESPACE, storage.context.user_id, value)
                append_event(session.connection, storage.context.tenant_id, "strategy_pool_updated",
                             {"user_id": storage.context.user_id})
            return _view(value, session, valid_ids)
    with _LOCK:
        current = _read(storage)
        value = _updated(current, storage, valid_ids, ids, revision, migrate)
        if value != current:
            path = _file(storage)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, json.dumps(value, ensure_ascii=False), mode=0o600)
        return _view(value, storage, valid_ids)
