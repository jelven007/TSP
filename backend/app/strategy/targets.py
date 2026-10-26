"""股票目标范围; 持久配置引用分组, 执行快照固定该次成员。"""
# ruff: noqa: RUF001 -- Localized business errors.
from __future__ import annotations

import copy
import json
from pathlib import Path

import polars as pl

from app.market_scope import STOCK_BOARDS, supported_symbol
from app.multitenancy.documents import TenantDocuments

# 同一监控轮次的多条策略共用自选快照, 文件签名变化时才重读。
_members_cache: dict[str, tuple[tuple[int, int] | None, list[dict]]] = {}


def group_ids(value: dict) -> list[str]:
    bf = value.get("basic_filter") or {}
    if bf.get("target_mode") != "selected" or not bf.get("include_custom"):
        return []
    return bf.get("watchlist_group_ids") or []


def validate(value: dict, available: set[str]) -> dict:
    value = copy.deepcopy(value)
    bf = value.get("basic_filter")
    if not isinstance(bf, dict):
        return value
    bf.pop("target_symbols", None)
    if "target_mode" not in bf:
        return value
    if bf["target_mode"] not in {"all", "selected"}:
        raise ValueError("目标股票模式无效")
    boards = bf.get("boards", [])
    ids = bf.get("watchlist_group_ids", [])
    if not isinstance(boards, list) or any(b not in STOCK_BOARDS for b in boards):
        raise ValueError("目标股票板块无效")
    if not isinstance(ids, list) or any(not isinstance(gid, str) for gid in ids):
        raise ValueError("目标自选分组格式无效")
    for key in ("include_custom", "exclude_st"):
        if key in bf and not isinstance(bf[key], bool):
            raise ValueError("目标股票开关必须为布尔值")
    if not set(group_ids(value)) <= available:
        raise ValueError("目标自选分组不存在，请重新选择")
    return value


def resolve(storage, value: dict, *, members: list[dict] | None = None) -> dict:
    value = copy.deepcopy(value)
    ids = set(group_ids(value))
    bf = value.get("basic_filter") or {}
    if bf.get("target_mode") != "selected" or not bf.get("include_custom"):
        return value
    if members is not None:
        symbols = [row["symbol"] for row in members if row["group_id"] in ids]
    elif isinstance(storage, TenantDocuments):
        with storage._transaction() as conn:
            rows = conn.execute(
                "SELECT symbol,group_id FROM business.watchlist_group_items WHERE group_id=ANY(%s)",
                (list(ids),),
            ).fetchall() if ids else []
        symbols = [row["symbol"] for row in rows]
    else:
        path = Path(storage) / "user_data" / "watchlist.parquet"
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            signature = None
        cached = _members_cache.get(str(path))
        if cached is None or cached[0] != signature:
            rows = pl.read_parquet(path).to_dicts() if signature else []
            _members_cache[str(path)] = (signature, rows)
        else:
            rows = cached[1]
        symbols = [row["symbol"] for row in rows
                   if ids.intersection(row.get("group_ids") or ([row["group_id"]] if row.get("group_id") else []))]
    bf["target_symbols"] = sorted({symbol for symbol in symbols if supported_symbol(symbol, "stock")})
    return value


def references(overrides: dict[str, dict], rules: list[dict]) -> dict[str, list[str]]:
    refs: dict[str, set[str]] = {}
    for sid, value in overrides.items():
        for gid in group_ids(value):
            refs.setdefault(gid, set()).add(sid)
    for rule in rules:
        if rule.get("type") == "strategy" and rule.get("scope") == "watchlist_group":
            for gid in [rule["group_id"]] if rule.get("group_id") else []:
                refs.setdefault(gid, set()).add(rule.get("strategy_id") or rule["id"])
    return {gid: sorted(ids) for gid, ids in refs.items()}


def file_references(storage: Path) -> dict[str, list[str]]:
    def documents(namespace):
        result = {}
        for path in (storage / "user_data" / namespace).glob("*.json"):
            result[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        return result
    return references(documents("strategy_overrides"), list(documents("monitor_rules").values()))


def sql_references(conn) -> dict[str, list[str]]:
    if conn.execute("SELECT to_regclass('business.documents') AS name").fetchone()["name"] is None:
        return {}
    rows = conn.execute(
        "SELECT namespace,id,value FROM business.documents WHERE namespace=ANY(%s)",
        (["strategy_overrides", "monitor_rules"],),
    ).fetchall()
    return references(
        {row["id"]: row["value"] for row in rows if row["namespace"] == "strategy_overrides"},
        [row["value"] for row in rows if row["namespace"] == "monitor_rules"]
        + [row["definition"] for row in conn.execute("SELECT definition FROM business.monitor_rules").fetchall()],
    )


def ensure_deletable(group_id: str, refs: dict[str, list[str]]) -> None:
    if refs.get(group_id):
        raise ValueError("该自选分组被策略引用，无法删除，请先解除策略引用")


def ensure_clearable(group_id: str | None, refs: dict[str, list[str]]) -> None:
    if group_id is None:
        if any(refs.values()):
            raise ValueError("存在被策略引用的自选分组，无法清空自选，请先解除策略引用")
    elif refs.get(group_id):
        raise ValueError("该自选分组被策略引用，无法清空，请先解除策略引用")


def annotate(groups: list[dict], refs: dict[str, list[str]]) -> list[dict]:
    return [{**group, **({"referenced_by": refs[group["id"]]} if refs.get(group["id"]) else {})}
            for group in groups]


def expression(df: pl.DataFrame, bf: dict) -> pl.Expr | None:
    if "target_mode" not in bf:
        return None
    exprs = []
    if bf["target_mode"] == "selected":
        candidates = []
        for board in bf.get("boards", []):
            prefixes = {"沪主板": ("60",), "深主板": ("00",),
                        "创业板": ("300", "301"), "科创板": ("688", "689")}[board]
            candidates.extend(pl.col("symbol").str.starts_with(prefix) for prefix in prefixes)
        if bf.get("include_custom"):
            candidates.append(pl.col("symbol").is_in(bf.get("target_symbols", [])))
        exprs.append(pl.any_horizontal(candidates) if candidates else pl.lit(False))
    if bf.get("exclude_st"):
        if "is_risk_warning" in df.columns:
            exprs.append(~pl.col("is_risk_warning").fill_null(False))
        elif "name" in df.columns:
            exprs.append(~pl.col("name").str.contains(r"(?i)\*?ST").fill_null(False))
    return pl.all_horizontal(exprs) if exprs else None
