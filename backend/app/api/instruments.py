"""管理员个股维表 CRUD。路径沿用公共数据管理转发及权限边界。"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.services.instrument_catalog import (
    CatalogConflictError,
    InstrumentCreate,
    InstrumentFields,
    list_instruments,
    mutate_instrument,
)


def require_catalog_admin(request: Request) -> None:
    from app.api.auth import COOKIE_NAME
    from app.config import authentication_required, settings
    from app.services import auth
    account = getattr(request.state, "account", None)
    if account is None and settings.service_role == "standalone":
        if not authentication_required():
            return  # 无认证的本地桌面空间归本机管理员。
        account = auth.current_user(request.cookies.get(COOKIE_NAME, ""))
    if not account or account.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以管理个股维表")


router = APIRouter(
    prefix="/api/data/instruments", tags=["data"], dependencies=[Depends(require_catalog_admin)],
)


@router.get("")
def catalog_list(
    request: Request, q: str = Query("", max_length=100),
    exchange: str = Query("", pattern="^(SH|SZ)?$"),
    board: str = Query("", pattern="^(沪主板|深主板|创业板|科创板)?$"),
    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200),
) -> dict:
    return list_instruments(
        request.app.state.repo, query=q, exchange=exchange, board=board, page=page, page_size=page_size,
    )


def _mutate(request, symbol, fields, *, create=False):
    try:
        return mutate_instrument(request.app.state.repo, symbol, fields, create=create)
    except CatalogConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="该股票不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("", status_code=201)
def catalog_create(request: Request, body: InstrumentCreate):
    return _mutate(request, body.symbol, body, create=True)


@router.put("/{symbol}")
def catalog_update(request: Request, symbol: str, body: InstrumentFields):
    return _mutate(request, symbol, body)


@router.delete("/{symbol}")
def catalog_delete(request: Request, symbol: str):
    _mutate(request, symbol, None)
    return {"deleted": symbol}
