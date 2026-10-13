import logging
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg import OperationalError
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field, field_validator

from app.cell.config import CellSettings
from app.cell.context import SYMBOL_PATTERN, AccessError, Role
from app.cell.database import Databases
from app.cell.identity import Identity
from app.cell.market import HttpMarketGateway, MarketGateway, UnavailableMarketGateway
from app.cell.watchlist import Watchlist

logger = logging.getLogger(__name__)

COLORS = {
    "sky", "blue", "indigo", "violet", "fuchsia", "rose", "orange", "amber",
    "lime", "emerald", "teal", "cyan",
}


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AddItems(BaseModel):
    symbols: list[Annotated[str, Field(pattern=SYMBOL_PATTERN)]] = Field(min_length=1, max_length=1000)
    note: str = Field(default="", max_length=2000)
    group_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=100)


class Group(BaseModel):
    name: str = Field(min_length=1, max_length=24)
    color: str = "sky"

    @field_validator("name")
    @classmethod
    def trim_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty_group_name")
        return value.strip()

    @field_validator("color")
    @classmethod
    def valid_color(cls, value: str) -> str:
        if value not in COLORS:
            raise ValueError("invalid_group_color")
        return value


class GroupOrder(BaseModel):
    ordered_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(max_length=1000)


class GroupAssignment(BaseModel):
    group_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(max_length=100)


class Member(BaseModel):
    role: Role


class NewToken(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    permissions: list[str] = Field(min_length=1, max_length=8)
    expires_in_days: int = Field(default=30, ge=1, le=90)


def credential(authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise AccessError(401, "authentication_required")
    value = authorization[7:]
    if not value or len(value) > 256:
        raise AccessError(401, "invalid_credentials")
    return value


Credential = Annotated[str, Depends(credential)]
Epoch = Annotated[int, Header(alias="X-Routing-Epoch", ge=1)]
Symbol = Annotated[str, Field(pattern=SYMBOL_PATTERN)]


def create_app(settings: CellSettings | None = None, market: MarketGateway | None = None) -> FastAPI:
    settings = settings or CellSettings()
    db = Databases(settings)
    identity = Identity(db)
    watchlist = Watchlist(db)
    owns_market = market is None
    market = market or (
        HttpMarketGateway(settings.market_url, settings.market_token.get_secret_value())
        if settings.market_url and settings.market_token.get_secret_value() else UnavailableMarketGateway()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.open()
        app.state.databases = db
        try:
            yield
        finally:
            db.close()
            if owns_market and isinstance(market, HttpMarketGateway):
                market.close()

    app = FastAPI(title="TSP Cell API", version="0.1.0", lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_methods=["GET","POST","PUT","DELETE"], allow_headers=["Authorization","Content-Type","X-Routing-Epoch"])

    @app.middleware("http")
    async def private_response(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AccessError)
    async def access_error(request: Request, exc: AccessError):
        return JSONResponse(
            {"detail": exc.code, "request_id": request.state.request_id}, status_code=exc.status
        )

    @app.exception_handler(PoolTimeout)
    @app.exception_handler(OperationalError)
    async def database_unavailable(request: Request, exc: Exception):
        logger.error("Cell database unavailable request_id=%s", request.state.request_id)
        return JSONResponse(
            {"detail": "database_unavailable", "request_id": request.state.request_id}, status_code=503
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok", "cell_id": settings.id}

    @app.get("/health/ready")
    def ready():
        db.check()
        return {"status": "ok", "cell_id": settings.id}

    @app.post("/api/auth/login")
    def login(body: Login):
        try:
            return identity.login(body.email, body.password)
        except ValueError as exc:
            raise AccessError(401, "invalid_credentials") from exc

    @app.post("/api/auth/logout", status_code=204)
    def logout(auth: Credential):
        identity.logout(auth)

    @app.get("/api/tenants")
    def directory(auth: Credential):
        return {"tenants": identity.directory(auth)}

    prefix = "/api/tenants/{tenant_id}"

    @app.get(prefix + "/members")
    def members(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "members:manage") as (ctx, conn):
            return {"members": identity.members(conn, ctx)}

    @app.put(prefix + "/members/{user_id}", status_code=204)
    def set_member(tenant_id: str, user_id: str, body: Member, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(
            auth, tenant_id, epoch, request.state.request_id, "members:manage", exclusive=True
        ) as (ctx, conn):
            identity.set_member(conn, ctx, user_id, body.role)

    @app.delete(prefix + "/members/{user_id}", status_code=204)
    def remove_member(tenant_id: str, user_id: str, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(
            auth, tenant_id, epoch, request.state.request_id, "members:manage", exclusive=True
        ) as (ctx, conn):
            identity.set_member(conn, ctx, user_id, None)

    @app.get(prefix + "/tokens")
    def tokens(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "tokens:manage") as (ctx, conn):
            return {"tokens": identity.tokens(conn, ctx)}

    @app.post(prefix + "/tokens", status_code=201)
    def create_token(tenant_id: str, body: NewToken, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(
            auth, tenant_id, epoch, request.state.request_id, "tokens:manage", exclusive=True
        ) as (ctx, conn):
            return identity.create_token(conn, ctx, body.name, body.permissions, body.expires_in_days)

    @app.delete(prefix + "/tokens/{token_id}", status_code=204)
    def revoke_token(tenant_id: str, token_id: str, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(
            auth, tenant_id, epoch, request.state.request_id, "tokens:manage", exclusive=True
        ) as (ctx, conn):
            identity.revoke_token(conn, ctx, token_id)

    @app.get(prefix + "/watchlist")
    def list_items(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:read") as (ctx, _):
            return watchlist.list(ctx)

    @app.post(prefix + "/watchlist")
    def add_items(tenant_id: str, body: AddItems, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.add(ctx, body.symbols, body.note, body.group_ids)

    @app.delete(prefix + "/watchlist")
    def clear_items(tenant_id: str, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.remove(ctx, None)

    @app.delete(prefix + "/watchlist/items/{symbol}")
    def remove_item(tenant_id: str, symbol: Symbol, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.remove(ctx, symbol)

    @app.put(prefix + "/watchlist/items/{symbol}/groups")
    def assign_item(tenant_id: str, symbol: Symbol, body: GroupAssignment, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.assign(ctx, symbol, body.group_ids)

    @app.post(prefix + "/watchlist/groups", status_code=201)
    def create_group(tenant_id: str, body: Group, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.group(ctx, None, body.name, body.color)

    @app.put(prefix + "/watchlist/groups/reorder")
    def reorder_groups(tenant_id: str, body: GroupOrder, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.reorder(ctx, body.ordered_ids)

    @app.put(prefix + "/watchlist/groups/{group_id}")
    def update_group(tenant_id: str, group_id: str, body: Group, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.group(ctx, group_id, body.name, body.color)

    @app.delete(prefix + "/watchlist/groups/{group_id}")
    def delete_group(tenant_id: str, group_id: str, epoch: Epoch, auth: Credential, request: Request):
        with identity.authorized(auth, tenant_id, epoch, request.state.request_id, "watchlist:write") as (ctx, _):
            return watchlist.delete_group(ctx, group_id)

    from app.cell.routes import router_for

    app.include_router(router_for(db, identity, market))
    if settings.static_dir:
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="frontend")
    return app
