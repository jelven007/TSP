"""个股维表管理员操作; 复用当前快照、历史状态和仓库缓存。"""
from __future__ import annotations

from contextlib import nullcontext
from datetime import date

import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from app.data_providers.instrument_status import current_a_share_identity
from app.market_time import cn_today
from app.services import instrument_sync


class InstrumentFields(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(min_length=1, max_length=100)
    listing_date: date | None = None
    region: str = Field(default="CN", max_length=100)
    industry: str | None = Field(default=None, max_length=100)
    total_shares: float | None = Field(default=None, gt=0)
    float_shares: float | None = Field(default=None, gt=0)
    tick_size: float | None = Field(default=None, gt=0)


class InstrumentCreate(InstrumentFields):
    symbol: str


class CatalogConflictError(ValueError):
    pass


def _path(repo):
    return repo.store.data_dir / "instruments" / "instruments.parquet"


def list_instruments(repo, *, query: str = "", exchange: str = "", page: int = 1, page_size: int = 50):
    frame = repo.get_instruments()
    if frame.is_empty():
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    if query:
        frame = frame.filter(
            pl.col("symbol").str.contains(query, literal=True)
            | pl.col("name").str.contains(query, literal=True)
        )
    if exchange:
        frame = frame.filter(pl.col("symbol").str.ends_with(f".{exchange}"))
    total = frame.height
    items = frame.sort("symbol").slice((page - 1) * page_size, page_size).to_dicts()
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def mutate_instrument(repo, symbol: str, fields: InstrumentFields | None, *, create: bool = False):
    identity = current_a_share_identity(symbol)
    if identity is None:
        raise ValueError("股票代码必须属于沪深 A 股, 例如 600000.SH 或 000001.SZ")
    symbol, code, exchange = identity
    with instrument_sync.catalog_lock, getattr(repo, "_write_lock", None) or nullcontext():
        frame = pl.read_parquet(_path(repo)) if _path(repo).exists() else pl.DataFrame()
        rows = {row["symbol"]: row for row in frame.iter_rows(named=True)}
        if create and symbol in rows:
            raise CatalogConflictError("该股票已存在")
        if not create and symbol not in rows:
            raise KeyError(symbol)
        as_of = cn_today()
        result = None
        if fields is None:
            del rows[symbol]
        else:
            from app.data_providers.instrument_status import (
                current_a_share_board,
                is_delisted_name,
                normalize_instrument_name,
            )
            values = fields.model_dump(exclude={"symbol"})
            values["name"] = normalize_instrument_name(values["name"])
            if not values["name"]:
                raise ValueError("股票名称不能为空")
            if is_delisted_name(values["name"]):
                raise ValueError("当前个股维表不接收退市或退市整理股票")
            if fields.listing_date and fields.listing_date > as_of:
                raise ValueError("上市日期不能晚于今天")
            if fields.total_shares and fields.float_shares and fields.float_shares > fields.total_shares:
                raise ValueError("流通股本不能大于总股本")
            values["listing_date"] = fields.listing_date.isoformat() if fields.listing_date else None
            result = {
                **rows.get(symbol, {}), **values, "symbol": symbol, "code": code,
                "exchange": exchange, "board": current_a_share_board(symbol), "type": "stock",
                "as_of": as_of, "snapshot_source": "manual", "catalog_source": "manual",
                "limit_up": None, "limit_down": None,
            }
            for field in instrument_sync._PERSISTENT_METADATA:
                result[f"{field}_source"] = "manual" if result.get(field) is not None else None
                result[f"{field}_as_of"] = as_of if result.get(field) is not None else None
            rows[symbol] = result
        candidate = pl.DataFrame(list(rows.values()), schema_overrides={
            "listing_date": pl.String,
            **{field: pl.Float64 for field in instrument_sync._PERSISTENT_METADATA if field != "listing_date"},
            **{f"{field}_as_of": pl.Date for field in instrument_sync._PERSISTENT_METADATA},
        }) if rows else frame.clear()
        instrument_sync.save_snapshot(repo.store.data_dir, candidate, as_of=as_of, source="manual")
    publish_catalog_change(repo, reason="instruments_manual")
    return result


def publish_catalog_change(repo, *, reason: str) -> str | None:
    from app.api.data import invalidate_data_cache
    from app.api.overview import invalidate_overview_cache
    from app.services.data_release import DataReleaseError, publish_data_release
    from app.services.screener import ScreenerService

    repo.refresh_current_instruments()
    invalidate_data_cache("instruments")
    invalidate_overview_cache()
    ScreenerService.clear_history_cache()
    try:
        return publish_data_release(
            repo.store.data_dir, reason=reason, write_lock=getattr(repo, "_write_lock", None),
        )["release_id"]
    except DataReleaseError:
        # 盘后 enriched 正在发布时, 公共 publisher 会在本轮提交后发布新维表。
        import logging
        logging.getLogger(__name__).warning("个股维表已保存, 公共数据发布等待当前管道完成")
        return None
