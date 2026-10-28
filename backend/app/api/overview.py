"""市场总览聚合 API。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request

from app.api import contracts
from app.services.public_query_cache import (
    invalidate_overview_cache as invalidate_overview_cache,
)
from app.services.public_query_cache import public_query_cache

router = APIRouter(prefix="/api/overview", tags=["overview"])

_CACHE_TTL = 5.0


def _build_overview(request: Request, as_of: date | None = None) -> dict:
    """装配市场总览(委托给 services.market_overview_builder,保持行为一致)。

    逻辑已抽离至 build_market_overview,以解耦对 Request 的依赖,
    使大盘复盘等无 Request 的调用方可复用同一装配逻辑。
    """
    from app.services.market_overview_builder import build_market_overview
    return build_market_overview(
        repo=request.app.state.repo,
        quote_service=getattr(request.app.state, "quote_service", None),
        depth_service=getattr(request.app.state, "depth_service", None),
        as_of=as_of,
    )


@router.get("/market", response_model=contracts.MarketOverviewResponse)
def market_overview(request: Request, as_of: date | None = None):
    """总览页单次请求聚合数据，避免前端拉全市场明细后再计算。"""
    cache_key = (
        str(request.app.state.repo.store.data_dir.resolve()),
        as_of.isoformat() if as_of else "latest",
    )
    return public_query_cache.get(
        "overview", cache_key, _CACHE_TTL, lambda: _build_overview(request, as_of),
    )
