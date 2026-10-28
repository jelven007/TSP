"""指数 API: 固定七只目录、日/周/月K与独立数据修正。"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Literal, Optional

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, field_validator

from app.api import contracts
from app.indicators.pipeline import compute_enriched
from app.market_time import cn_today
from app.services import index_sync, kline_sync, preferences, trading_day
from app.services.index_const import page_index_catalog
from app.tickflow.capabilities import Cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/index", tags=["index"])

# 指数分钟结果进程内缓存: 吸收板块切换卡片/指数页轮询与重挂载的重复请求
_INDEX_MINUTE_CACHE_TTL = 10.0
_INDEX_MINUTE_CACHE_MAX = 32
_index_minute_cache: dict[tuple[str, str, str], tuple[float, pl.DataFrame]] = {}
_index_minute_cache_lock = threading.Lock()


def _index_info(repo, symbol: str) -> dict:
    pinned = next((row for row in page_index_catalog() if row["symbol"] == symbol), None)
    if pinned:
        return pinned
    df = repo.get_index_instruments()
    if df.is_empty() or "symbol" not in df.columns:
        return {}
    hit = df.filter(pl.col("symbol") == symbol).head(1)
    if hit.is_empty():
        return {}
    return hit.to_dicts()[0]


@router.get("/catalog")
def index_catalog():
    return {"rows": page_index_catalog(), "count": 7}


@router.get("/kline", response_model=contracts.IndexKlineResponse)
def get_index_kline(
    request: Request,
    symbol: str,
    period: Literal["1d", "1w", "1mo"] = "1d",
    start_date: date | None = None,
    end_date: date | None = None,
):
    from app.services.index_kline import bucket
    end = end_date or cn_today()
    start = start_date or end - timedelta(days={"1d": 180, "1w": 1095, "1mo": 3650}[period])
    if start > end:
        raise HTTPException(400, "结束日期不能早于起始日期")
    repo = request.app.state.repo
    info = _index_info(repo, symbol)
    rows = repo.get_index_period(symbol, period, start, end).to_dicts()
    if period == "1d":
        from app.api.kline import _maybe_inject_live_candle
        rows = _maybe_inject_live_candle(request, symbol, rows, "index")
        rows = [row for row in rows if start.isoformat() <= str(row["date"])[:10] <= end.isoformat()]
    return {
        "symbol": symbol, "name": info.get("name"), "index_info": info,
        "period": period, "rows": rows, "source": "local" if rows else "none",
        "start_date": bucket(start, period).isoformat(), "end_date": end.isoformat(),
    }


class IndexRepairRequest(BaseModel):
    start_date: date
    end_date: date | None = None
    period: Literal["1d", "1w", "1mo"] = "1d"
    mode: Literal["full", "missing"] = "full"

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def strict_date(cls, value):
        if value is None:
            return value
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("日期须为 YYYY-MM-DD")
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("日期须为 YYYY-MM-DD")
        return parsed


@router.post("/repair")
async def repair_index(request: Request, body: IndexRepairRequest):
    import asyncio

    from app.api.kline import _long_task_executor, _period_tasks
    from app.services import index_kline, kline_periods
    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )
    end = body.end_date or cn_today()
    if body.start_date > end or end > cn_today():
        raise HTTPException(400, "日期范围须按先后排序且不能晚于今天")
    capset = request.app.state.capabilities
    if body.period == "1d":
        if preferences.get_daily_data_provider() == "tickflow":
            if not capset.has(Cap.KLINE_DAILY_BATCH):
                raise HTTPException(403, "当前K线数据源不具备日K能力")
        else:
            try:
                kline_periods._period_provider("1d")
            except ValueError as exc:
                raise HTTPException(403, str(exc)) from exc
    job_id, created = job_store.create(kind="index_repair", request={
        "start_date": str(body.start_date), "end_date": str(end),
        "period": body.period, "mode": body.mode,
    })
    if not created:
        return {"status": "reused", "job_id": job_id}

    async def task():
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行, 请稍后再试")
            return
        def progress(stage, pct, message, **kwargs):
            job_store.progress(job_id, stage, pct, message, **kwargs)
        def run():
            return index_kline.repair(
                request.app.state.repo, capset, body.start_date, end,
                period=body.period, mode=body.mode, on_progress=progress,
                quote_service=getattr(request.app.state, "quote_service", None),
            )
        try:
            result = await asyncio.get_running_loop().run_in_executor(_long_task_executor, run_with_capacity, job_id, run)
            if result.get("error"):
                job_store.fail(job_id, result["error"])
            else:
                progress("done", 100, result["message"], stage_pct=100)
                job_store.succeed(job_id, result)
        except JobCancelledError:
            job_store.cancelled(job_id)
        except Exception as exc:
            logger.exception("指数修正失败: job_id=%s", job_id)
            job_store.fail(job_id, str(exc))
        finally:
            release_run_slot(job_id)
    running = asyncio.create_task(task())
    _period_tasks.add(running)
    running.add_done_callback(_period_tasks.discard)
    return {"status": "started", "job_id": job_id}


@router.get("/daily", response_model=contracts.IndexDailyResponse)
def get_index_daily(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    days: int = Query(120, ge=10, le=2000),
    start_date: Optional[str] = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: Optional[str] = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
):
    """读取指数日 K。指数数据使用独立 kline_index_* parquet。"""
    repo = request.app.state.repo
    # 未传 end_date 时用北京今天。实时注入只在内存缓存命中时补当日 K,
    # 缓存冷时 parquet 当日行能否进结果取决于这个窗口右端。
    end = date.fromisoformat(end_date) if end_date else cn_today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)
    info = _index_info(repo, symbol)

    df = repo.get_index_daily(symbol, start, end)
    if not df.is_empty():
        from app.api.kline import _maybe_inject_live_candle
        rows = _maybe_inject_live_candle(request, symbol, df.to_dicts(), "index")
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "index_enriched"}

    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    try:
        raw = kline_sync.sync_daily_batch([symbol], count=days + 150)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"TickFlow fetch failed: {e}") from e
    if raw.is_empty():
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}

    enriched = compute_enriched(raw, factors=None, instruments=None)
    rows = enriched.filter((pl.col("date") >= start) & (pl.col("date") <= end)).to_dicts()
    return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "live"}


@router.get("/minute", response_model=contracts.IndexMinuteResponse)
def get_index_minute(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    trade_date: date | None = Query(None, alias="date", description="交易日期, 休市时默认最近本地指数交易日"),
    with_prev_close: bool = False,
):
    """实时读取指数分钟 K。不写入股票分钟 parquet。

    历史深度由当前分钟源决定, 显式日期不会因空数据而替换。
    未指定日期且确认休市时使用最近本地指数日 K 的日期。
    结果带 10s 进程内缓存, 不同指数、日期和分钟源分别缓存。
    """
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    info = _index_info(repo, symbol)
    today = cn_today()
    day = trade_date or today
    if trade_date is None and trading_day.is_trading_day() is False:
        daily = repo.get_index_daily(symbol, today - timedelta(days=366), today, columns=["date"])
        if not daily.is_empty():
            day = daily["date"].max() or today
    if day > today:
        return {
            "symbol": symbol,
            "name": info.get("name"),
            "index_info": info,
            "date": str(day),
            "rows": [],
            "source": "future",
        }
    cache_key = (symbol, day.isoformat(), preferences.get_minute_data_provider())
    now = time.monotonic()
    with _index_minute_cache_lock:
        hit = _index_minute_cache.get(cache_key)
    if hit is not None and now - hit[0] < _INDEX_MINUTE_CACHE_TTL:
        df = hit[1]
    else:
        df = kline_sync.fetch_minute_single(symbol, day, asset_type="index", capset=capset)
        with _index_minute_cache_lock:
            _index_minute_cache[cache_key] = (time.monotonic(), df)
            while len(_index_minute_cache) > _INDEX_MINUTE_CACHE_MAX:
                oldest = min(_index_minute_cache, key=lambda k: _index_minute_cache[k][0])
                del _index_minute_cache[oldest]
    reference = {}
    if with_prev_close:
        previous = repo.get_index_daily(symbol, day - timedelta(days=30), day - timedelta(days=1), columns=["date", "close"])
        reference["prev_close"] = previous["close"][-1] if not previous.is_empty() and "close" in previous.columns else None
    return {
        "symbol": symbol,
        "name": info.get("name"),
        "index_info": info,
        "date": str(day),
        "rows": df.to_dicts(),
        "source": "live" if not df.is_empty() else "none",
        **reference,
    }


@router.post("/sync_instruments")
def sync_index_instruments(request: Request):
    """同步 CN_Index 指数标的列表。"""
    repo = request.app.state.repo
    count = index_sync.sync_index_instruments(repo)
    return {"status": "ok", "count": count}


@router.post("/sync_daily")
def sync_index_daily(
    request: Request,
    days: int = Query(365, ge=30, le=5000),
):
    """同步指数日K到独立 parquet。"""
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        raise HTTPException(status_code=403, detail="需要 Pro+ 权限 (batch K-line)")
    end = datetime.now()
    start = end - timedelta(days=days)
    count = index_sync.sync_index_instruments(repo)
    rows = index_sync.sync_and_persist_index_daily(repo, capset, start_date=start, end_date=end)
    return {"status": "ok", "index_count": count, "rows_written": rows}
