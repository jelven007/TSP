"""批次登记 API — 薄"批次"页 (持仓提醒), 只做胶水, 不含会计语义。

映射/校验/持久化在 strategy.lots 域; 写完派生规则后复用 monitor_rules 的 _sync_engine 同步引擎。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.multitenancy.documents import TenantDocuments, private_storage
from app.strategy import lots as lots_domain
from app.strategy import monitor_rules

router = APIRouter(prefix="/api/lots", tags=["lots"])

# 批次 + 派生规则 + 引擎重载的跨请求互斥; 规则全部校验通过才落盘, 避免半成品 (镜像 watchlist 服务层)。
_write_lock = threading.Lock()


def _data_dir(request: Request) -> Path:
    return private_storage(request) or request.app.state.repo.store.data_dir


def _resolve_asset_type(request: Request, symbol: str) -> str:
    """按 symbol 解析资产类型 (stock/index); 解析失败默认 stock。"""
    repo = getattr(request.app.state, "repo", None)
    try:
        return repo.resolve_asset_type(symbol) if repo is not None else "stock"
    except Exception:
        logging.getLogger(__name__).warning(
            "resolve_asset_type failed for %s, falling back to stock", symbol, exc_info=True
        )
        return "stock"


class LotModel(BaseModel):
    id: str | None = None
    batch_id: str | None = None
    symbol: str
    qty: float = 0
    cost_price: float = 0
    buy_date: str | None = None
    target_pct: float = 0
    stop_pct: float = 0
    remind_date: str | None = None
    lead_days: int = 1


class BatchModel(BaseModel):
    name: str = Field(default="", max_length=80)
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    stocks: list[LotModel] = Field(min_length=1, max_length=100)


@contextmanager
def _write_unit(data_dir):
    if isinstance(data_dir, TenantDocuments):
        with data_dir.transaction() as unit:
            yield unit
        return
    # 本地文件模式也回滚全部相关文件; SQL 模式由租户写事务实现隔离和原子性。
    from app.services.fs_utils import atomic_write_text

    with _write_lock:
        dirs = [data_dir / "user_data" / name for name in ("lots", "monitor_rules")]
        before = {p: p.read_text(encoding="utf-8") for d in dirs for p in d.glob("*.json")}
        try:
            yield data_dir
        except Exception:
            for d in dirs:
                for p in d.glob("*.json"):
                    if p not in before:
                        p.unlink()
            for p, text in before.items():
                atomic_write_text(p, text)
            raise


def _reload_engine(request: Request) -> None:
    """批次规则保存/删除后重载引擎 — 复用监控规则 API 的共享重载 (含指数纠正)。"""
    from app.api.monitor_rules import _sync_engine

    _sync_engine(request)


def _sync_lots(request: Request, data_dir, lots: list[dict]) -> None:
    """写批次文件 + 同步其两条派生监控规则 + 重载引擎。

    派生规则继承用户默认推送渠道 (webhook_default_channels), 否则批次告警会静默只走应用内。
    """
    from app.services import preferences

    default_channels = (
        data_dir.get("notification_preferences", "default", {}).get("default_channels", [])
        if isinstance(data_dir, TenantDocuments) else preferences.get_webhook_default_channels()
    )
    writes, deletes = [], []
    reminders_enabled = (
        data_dir.get("lot_monitor_settings", "default", {}).get("enabled", True)
        if isinstance(data_dir, TenantDocuments) else True
    )
    existing_lots = {item["id"]: item for item in lots_domain.load_all(data_dir)}
    for lot in lots:
        old = existing_lots.get(lot["id"])
        if old:
            lot["created_at"] = old["created_at"]
            # 股票编辑不能移动/丢失所属批次; 兼容旧客户端未传 batch_id。
            lot["batch_id"] = old.get("batch_id")
        elif lot.get("batch_id") and not any(b["id"] == lot["batch_id"] for b in lots_domain.load_batches(data_dir)):
            raise HTTPException(400, "所属批次不存在")
        # 指数资产类型解析 (止盈止损价格规则须走对应资产监控轮才会触发)
        asset_type = _resolve_asset_type(request, lot["symbol"])
        price_rule, date_rule = lots_domain.lot_to_rules(lot) if reminders_enabled else (None, None)
        rules_to_write: list[dict] = []
        rules_to_delete: list[str] = []
        for rid, rule in ((f"{lot['id']}_p", price_rule), (f"{lot['id']}_d", date_rule)):
            if rule is None:
                rules_to_delete.append(rid)
                continue
            rule["asset_type"] = asset_type
            rule.setdefault("webhook_channels", list(default_channels))
            # 保留旧 created_at, 避免编辑批次后派生规则在监控中心列表跳位
            existing = monitor_rules.load_one(data_dir, rid)
            if existing and existing.get("created_at"):
                rule["created_at"] = existing["created_at"]
            try:
                monitor_rules.validate(rule)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
            rules_to_write.append(monitor_rules.normalize(rule))
        writes.extend(rules_to_write)
        deletes.extend(rules_to_delete)
    # 所有股票的派生规则全部校验通过后才开始写入。
    for lot in lots:
        lots_domain.save_one(data_dir, lot)
    for rid in deletes:
        monitor_rules.delete_one(data_dir, rid, managed=True)
    for rule in writes:
        monitor_rules.save_one(data_dir, rule)


def sync_lot(request: Request, lot: dict) -> None:
    with _write_unit(_data_dir(request)) as data_dir:
        _sync_lots(request, data_dir, [lot])
    _reload_engine(request)


@router.get("/expiry-options")
def expiry_options(base_date: Annotated[date | None, Query()] = None):
    from app.market_time import cn_today

    return lots_domain.expiry_options(base_date or cn_today())


@router.get("/batches")
def list_batches(request: Request):
    data_dir = _data_dir(request)
    unit = data_dir.transaction(write=False) if isinstance(data_dir, TenantDocuments) else _write_lock
    with unit as store:
        store = store if isinstance(data_dir, TenantDocuments) else data_dir
        return {"batches": lots_domain.load_batches(store), "next_number": lots_domain.next_batch_number(store)}


@router.post("/batches")
def create_batch(batch_in: BatchModel, request: Request):
    from app.market_scope import supported_symbol

    stocks = []
    for incoming in batch_in.stocks:
        lot = incoming.model_dump(exclude={"id", "batch_id"})
        if not supported_symbol(lot["symbol"], "stock"):
            raise HTTPException(400, "持仓提醒仅支持股票")
        try:
            lots_domain.validate_lot(lot)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        stocks.append(lot)
    if len({s["symbol"] for s in stocks}) != len(stocks):
        raise HTTPException(400, "同一批次不能重复添加同一股票")
    name = batch_in.name.strip()
    fingerprint = hashlib.sha256(json.dumps(
        {"name": name, "stocks": stocks}, sort_keys=True, ensure_ascii=False, allow_nan=False,
    ).encode()).hexdigest()
    with _write_unit(_data_dir(request)) as data_dir:
        existing = next((b for b in lots_domain.load_batches(data_dir)
                         if b.get("request_id") == batch_in.request_id), None)
        if existing:
            if existing.get("fingerprint") != fingerprint:
                raise HTTPException(409, "该创建请求已保存, 请刷新后重新新增批次")
            return {"ok": True, "batch": existing,
                    "lots": [s for s in lots_domain.load_all(data_dir) if s.get("batch_id") == existing["id"]]}
        try:
            batch = lots_domain.save_batch(data_dir, {
                "name": name, "request_id": batch_in.request_id, "fingerprint": fingerprint,
            })
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        lots = [lots_domain.normalize_lot({
            **s, "id": f"lot_{int(time.time() * 1000):x}_{secrets.token_hex(4)}", "batch_id": batch["id"],
        }) for s in stocks]
        _sync_lots(request, data_dir, lots)
    _reload_engine(request)
    return {"ok": True, "batch": batch, "lots": lots}


@router.delete("/batches/{batch_id}")
def delete_batch(batch_id: str, request: Request):
    if not re.fullmatch(r"[0-9]{10}", batch_id):
        raise HTTPException(400, "批次号非法")
    with _write_unit(_data_dir(request)) as data_dir:
        if not any(batch["id"] == batch_id for batch in lots_domain.load_batches(data_dir)):
            raise HTTPException(404, "批次不存在")
        members = [lot for lot in lots_domain.load_all(data_dir) if lot.get("batch_id") == batch_id]
        # 删除前统一校验存储的成员ID, 禁止路径越界或删到其他记录。
        for lot in members:
            lot_id = lot.get("id")
            if (not isinstance(lot_id, str) or not monitor_rules.ID_RE.fullmatch(lot_id)
                    or len(lot_id) > 38):
                raise HTTPException(400, "批次股票 id 非法")
        for lot in members:
            monitor_rules.delete_one(data_dir, f"{lot['id']}_p", managed=True)
            monitor_rules.delete_one(data_dir, f"{lot['id']}_d", managed=True)
            lots_domain.delete_one(data_dir, lot["id"])
        lots_domain.delete_batch(data_dir, batch_id)
    _reload_engine(request)
    return {"ok": True, "deleted_lots": len(members)}


@router.get("")
def list_lots(request: Request):
    return {"lots": lots_domain.load_all(_data_dir(request))}


@router.post("")
def upsert_lot(lot_in: LotModel, request: Request):
    """新建/更新一个批次。id 缺省时服务端生成 (紧凑, 保证 {id}_p/_d 规则 id ≤ 40 字符)。"""
    lot = lot_in.model_dump()
    from app.market_scope import supported_symbol
    if not supported_symbol(lot.get("symbol"), "stock"):
        raise HTTPException(status_code=400, detail="持仓提醒仅支持股票")
    if not lot.get("id"):
        lot["id"] = f"lot_{int(time.time() * 1000):x}_{secrets.token_hex(2)}"
    try:
        lots_domain.validate_lot(lot)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    lot = lots_domain.normalize_lot(lot)
    sync_lot(request, lot)
    return {"ok": True, "lot": lot}


@router.delete("/{lot_id}")
def delete_lot(lot_id: str, request: Request):
    if not monitor_rules.ID_RE.match(lot_id):
        raise HTTPException(status_code=400, detail="批次 id 非法")
    data_dir = _data_dir(request)
    with _write_unit(data_dir) as data_dir:
        deleted = lots_domain.delete_one(data_dir, lot_id)
        # 两条派生规则都要删 (用 or 会短路跳过第二条)
        deleted_p = monitor_rules.delete_one(data_dir, f"{lot_id}_p", managed=True)
        deleted_d = monitor_rules.delete_one(data_dir, f"{lot_id}_d", managed=True)
    if deleted or deleted_p or deleted_d:
        _reload_engine(request)
    return {"ok": True}
