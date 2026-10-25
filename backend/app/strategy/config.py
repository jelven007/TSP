"""策略配置持久化 — 读写用户覆盖值。

职责: 将每个策略的用户定制设置（基础参数、策略参数、评分、买卖信号）持久化到 JSON。
不知道: 引擎、AI、前端、回测。
存储: data/user_data/strategy_overrides/{strategy_id}.json
"""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from app.market_scope import clean_market_config
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

# 进程内缓存: 监控引擎每轮对每条策略规则调用 load_override, 每次读盘+parse 纯重复;
# override 仅在用户编辑时变化, 以 (mtime_ns, size) 签名判断是否重读。
# 键为 override 文件路径 (进程内可能有多个 data_dir, 如测试)。
_override_cache: dict[str, dict] = {}
_override_cache_sig: dict[str, tuple[int, int]] = {}


def _invalidate_override_cache(path: Path) -> None:
    key = str(path)
    _override_cache.pop(key, None)
    _override_cache_sig.pop(key, None)


def _overrides_dir(data_dir: Path) -> Path:
    d = data_dir / "user_data" / "strategy_overrides"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(data_dir: Path, strategy_id: str, *, ensure_dir: bool = True) -> Path:
    # ensure_dir=False 供热路径读取: mkdir 系统调用在 Windows 上 ~0.07ms,
    # 读缓存命中时跳过它 (目录由写路径保证存在)。
    if ensure_dir:
        d = _overrides_dir(data_dir)
    else:
        d = data_dir / "user_data" / "strategy_overrides"
    return d / f"{strategy_id}.json"


def load_override(data_dir: Path, strategy_id: str) -> dict:
    """读取策略的用户覆盖配置，不存在返回空 dict (带 mtime 签名缓存, 返回深拷贝)"""
    from app.multitenancy.documents import TenantDocuments
    from app.strategy.targets import resolve

    if isinstance(data_dir, TenantDocuments):
        return resolve(data_dir, clean_market_config(data_dir.get("strategy_overrides", strategy_id, {})))
    p = _path(data_dir, strategy_id, ensure_dir=False)
    key = str(p)
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        _invalidate_override_cache(p)
        return {}
    cached = _override_cache.get(key)
    if cached is not None and sig == _override_cache_sig.get(key):
        return resolve(data_dir, cached)
    try:
        data = clean_market_config(json.loads(p.read_text(encoding="utf-8")))
        # 清理 basic_filter 中值为 None/空的键（避免固化无意义的空值）
        bf = data.get("basic_filter")
        if isinstance(bf, dict):
            cleaned = {k: v for k, v in bf.items() if v is not None}
            if cleaned:
                data["basic_filter"] = cleaned
            else:
                del data["basic_filter"]
        _override_cache[key] = data
        _override_cache_sig[key] = sig
    except Exception as e:
        logger.warning("load override %s failed: %s", strategy_id, e)
        return {}
    return resolve(data_dir, data)


def save_override(data_dir: Path, strategy_id: str, overrides: dict) -> None:
    """保存策略的用户覆盖配置（全量覆盖写）"""
    from app.multitenancy.documents import TenantDocuments
    from app.strategy.overlay import effective_conditions
    from app.strategy.targets import validate

    overrides = copy.deepcopy(overrides)
    if "overlay_filter" in overrides:
        overrides["overlay_filter"] = effective_conditions(overrides)

    if isinstance(data_dir, TenantDocuments):
        with data_dir.transaction() as session:
            available = {row["id"] for row in session.connection.execute(
                "SELECT id FROM business.watchlist_groups",
            ).fetchall()}
            session.put("strategy_overrides", strategy_id, clean_market_config(validate(overrides, available)))
        return
    from app.services.watchlist import _LOCK
    with _LOCK:
        groups_path = data_dir / "user_data" / "watchlist_groups.json"
        groups = json.loads(groups_path.read_text(encoding="utf-8")) if groups_path.exists() else []
        overrides = validate(overrides, {group["id"] for group in groups})
        p = _path(data_dir, strategy_id)
        atomic_write_text(p, json.dumps(clean_market_config(overrides), ensure_ascii=False, indent=2))
        _invalidate_override_cache(p)


def delete_override(data_dir: Path, strategy_id: str) -> None:
    """删除策略的用户覆盖配置（重置为默认值）"""
    from app.multitenancy.documents import TenantDocuments

    if isinstance(data_dir, TenantDocuments):
        data_dir.delete("strategy_overrides", strategy_id)
        return
    p = _path(data_dir, strategy_id)
    _invalidate_override_cache(p)
    if p.exists():
        p.unlink()


def list_overrides(data_dir: Path) -> dict[str, dict]:
    """返回所有策略的覆盖配置 {strategy_id: overrides}"""
    from app.multitenancy.documents import TenantDocuments

    if isinstance(data_dir, TenantDocuments):
        from app.strategy.targets import group_ids, resolve
        with data_dir.transaction(write=False) as session:
            records = session.records("strategy_overrides")
            selected = {gid for row in records for gid in group_ids(row["value"])}
            members = session.connection.execute(
                "SELECT symbol,group_id FROM business.watchlist_group_items WHERE group_id=ANY(%s)",
                (list(selected),),
            ).fetchall() if selected else []
            return {row["id"]: resolve(session, clean_market_config(row["value"]), members=members)
                    for row in records}
    d = _overrides_dir(data_dir)
    result: dict[str, dict] = {}
    for f in d.glob("*.json"):
        try:
            sid = f.stem
            result[sid] = load_override(data_dir, sid)
        except Exception:
            continue
    return result
