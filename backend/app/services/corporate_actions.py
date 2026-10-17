"""Normalized public company-action storage; no account data or network on reads."""
from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

import polars as pl

from app.data_providers.corporate_actions import CorporateAction
from app.services.fs_utils import atomic_write_parquet

_LOCK = threading.RLock()
MAX_EVENTS = 100_000


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / "corporate_actions" / "all.parquet"


@lru_cache(maxsize=4)
def _read(path: str, mtime: int, size: int) -> dict[str, list[CorporateAction]]:
    del mtime, size
    rows = pl.read_parquet(path)
    if rows.height > MAX_EVENTS:
        raise ValueError("company-action dataset exceeds capacity")
    grouped = {}
    for row in rows.to_dicts():
        action = CorporateAction.model_validate(row)
        grouped.setdefault(action.symbol, []).append(action)
    return grouped


def load_actions(data_dir: Path, symbols: set[str] | None = None) -> list[CorporateAction]:
    path = _path(data_dir)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return []
    grouped = _read(str(path), stat.st_mtime_ns, stat.st_size)
    return [action.model_copy() for symbol in (grouped if symbols is None else symbols)
            for action in grouped.get(symbol, [])]


def save_actions(data_dir: Path, actions: list[CorporateAction]) -> int:
    """Merge complete validated observations atomically; failed fetches never call this."""
    with _LOCK:
        existing = {action.id: action for action in load_actions(data_dir)}
        seen = set()
        for action in actions:
            action = CorporateAction.model_validate(action)
            if action.id in seen:
                raise ValueError("duplicate company-action ID")
            seen.add(action.id)
            previous = existing.get(action.id)
            if previous and (previous.symbol, previous.source, previous.ex_date) != (
                action.symbol, action.source, action.ex_date,
            ):
                raise ValueError("company-action identity changed")
            existing[action.id] = action
        if len(existing) > MAX_EVENTS:
            raise ValueError("company-action dataset exceeds capacity")
        keys = [(action.symbol, action.asset_type, action.ex_date) for action in existing.values()]
        if len(keys) != len(set(keys)):
            raise ValueError("conflicting company-action sources for the same ex-date")
        if existing:
            path = _path(data_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_parquet(pl.from_dicts([
                action.model_dump() for action in sorted(existing.values(), key=lambda x: (x.ex_date, x.id))
            ]), path)
            _read.cache_clear()
        return len(actions)


def sync_actions(data_dir: Path, symbols: list[str]) -> int:
    from app.data_providers import custom, get_provider
    from app.services import preferences

    name = preferences.get_corporate_actions_provider()
    if not custom.provider_has_dataset(name, "corporate_actions"):
        raise ValueError(f"数据源 {name} 不提供公司行为事件")
    provider = get_provider(name)
    method = getattr(provider, "get_corporate_actions", None)
    if not callable(method):
        raise ValueError(f"数据源 {name} 缺少公司行为接口")
    rows = method(symbols=symbols, start_time=None, end_time=None, asset_type="stock")
    if not isinstance(rows, pl.DataFrame):
        raise ValueError("company-action provider must return a DataFrame")
    actions = [CorporateAction.model_validate(row) for row in rows.to_dicts()]
    if any(action.symbol not in symbols for action in actions):
        raise ValueError("unexpected company-action symbol")
    return save_actions(data_dir, actions)
