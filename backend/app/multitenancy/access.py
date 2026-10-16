"""Same-origin protection and system permissions for full-product routes."""
# ruff: noqa: RUF001 -- Chinese account messages use localized punctuation.
from __future__ import annotations

import uuid
from urllib.parse import urlsplit

from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from app.api.auth import COOKIE_NAME
from app.cell.context import AccessError

PUBLIC_ENDPOINTS = frozenset({
    "/api/auth/status", "/api/auth/login", "/api/auth/register/code", "/api/auth/register",
    "/api/auth/recovery/code", "/api/auth/recovery/reset", "/api/auth/logout", "/api/health",
})
ADMIN_PREFIXES = {
    "/api/settings": "settings:manage",
    "/api/data": "data:manage",
    "/api/pipeline": "data:manage",
    "/api/admin": "users:manage",
    "/api/observability": "settings:manage",
    "/api/capabilities/redetect": "settings:manage",
}
PRIVATE_PREFIXES = (
    "/api/watchlist", "/api/strategy", "/api/strategies", "/api/factors",
    "/api/signals", "/api/custom-signals", "/api/screener", "/api/mining", "/api/backtest", "/api/paper",
    "/api/monitor-rules", "/api/lots", "/api/alerts", "/api/events",
    "/api/custom/assistant", "/api/stock-analysis", "/api/financials/reports",
    "/api/market-recap/reports",
)
PUBLIC_QUERY_POSTS = frozenset({
    "/api/kline/instruments/names", "/api/kline/daily-batch", "/api/kline/minute-batch",
})


def csrf(request: Request) -> None:
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    origin = urlsplit(request.headers.get("origin", ""))
    # HTTP clients must explicitly supply the same Origin as the browser.
    expected = request.url
    if origin.scheme != expected.scheme or origin.netloc != expected.netloc:
        raise AccessError(403, "请求来源校验失败")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise AccessError(403, "请求来源校验失败")


async def middleware(request: Request, call_next):
    request.state.request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
    path = request.url.path
    if not path.startswith("/api/") or request.method == "OPTIONS":
        return await call_next(request)
    try:
        service = request.app.state.account_identity
        credential = request.headers.get("authorization", "")
        if not credential:
            csrf(request)
        if path not in PUBLIC_ENDPOINTS:
            if credential:
                from app.multitenancy.tokens import Tokens

                scheme, _, value = credential.partition(" ")
                if scheme.lower() != "bearer":
                    raise AccessError(401, "请使用租户绑定的有效凭据")
                account, token_context = await run_in_threadpool(
                    Tokens(service).authenticate, value, request.headers.get("x-tenant-id"),
                    request.state.request_id, request.method, path,
                )
                request.state.tenant_context = token_context
                request.state.api_credential = value
            else:
                account = await run_in_threadpool(
                    service.current_user, request.cookies.get(COOKIE_NAME, ""),
                )
            expected_scope = request.headers.get("x-tsp-account-scope") or request.query_params.get("account_scope")
            current_scope = f"tsp-v1:{account['id']}:{account['default_tenant_id'] or 'market'}:{account['auth_version']}"
            if expected_scope and expected_scope != current_scope:
                raise AccessError(401, "账户已切换，请重新登录")
            request.state.account = account
            permission = "business:read" if request.method in {"GET", "HEAD"} else "business:write"
            for prefix, admin_permission in ADMIN_PREFIXES.items():
                if path == prefix or path.startswith(prefix + "/"):
                    permission = admin_permission
                    break
            if path.startswith("/api/financials/sync/") or path.startswith("/api/kline/sync"):
                permission = "data:manage"
            if request.method not in {"GET", "HEAD"}:
                if path.startswith("/api/kline/") and path not in PUBLIC_QUERY_POSTS:
                    permission = "data:manage"
                if path.startswith("/api/index/sync") or path in {
                    "/api/regime/recompute", "/api/regime/mainline/recompute",
                }:
                    permission = "data:manage"
                if path == "/api/ext-data" or path.startswith("/api/ext-data/"):
                    permission = "settings:manage"
            if path.startswith("/api/ext-data/") and path.endswith("/api-key"):
                permission = "settings:manage"
            if path.startswith("/api/analysis-menus/") and request.method not in {"GET", "HEAD"}:
                permission = "settings:manage"
            account_only = path.startswith("/api/auth/") or path.startswith("/api/account/")
            if account_only:
                permission = "account:manage"
            if permission not in account["permissions"]:
                raise AccessError(403, "无权使用该功能")
            from app.multitenancy.platform import management_path, market_read_path

            if management_path(request.method, path):
                response = await request.app.state.platform_admin.forward(request)
                response.headers["Cache-Control"] = "no-store"
                return response
            selected = request.headers.get("x-tenant-id") or account["default_tenant_id"]
            private = any(path == prefix or path.startswith(prefix + "/") for prefix in PRIVATE_PREFIXES)
            system_only = permission in {"settings:manage", "data:manage", "users:manage"}
            if not credential and (private or (selected and not account_only and not system_only)):
                request.state.tenant_context = await run_in_threadpool(
                    service.context, account, selected, request.state.request_id,
                )
            if market_read_path(request.method, path):
                response = await request.app.state.platform_reads.forward(request)
                response.headers["Cache-Control"] = "no-store"
                return response
        elif credential:
            raise AccessError(403, "该接口不接受 API Token")
        from app.factors.registry import private_factor_registry
        from app.multitenancy.documents import TenantDocuments

        context = getattr(request.state, "tenant_context", None)
        definitions = []
        if context:
            documents = TenantDocuments(service.db, context)
            request.state.private_documents = documents
            definitions = await run_in_threadpool(documents.values, "custom_factors")
        with private_factor_registry(definitions):
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        account = getattr(request.state, "account", None)
        if account:
            response.headers["X-TSP-Account-Scope"] = (
                f"tsp-v1:{account['id']}:{account['default_tenant_id'] or 'market'}:{account['auth_version']}"
            )
        return response
    except AccessError as exc:
        return JSONResponse(
            status_code=exc.status, content={"detail": exc.code},
            headers={"Cache-Control": "no-store"},
        )


async def access_error(request: Request, exc: AccessError):
    return JSONResponse(
        status_code=exc.status, content={"detail": exc.code},
        headers={"Cache-Control": "no-store"},
    )


def verify_live_access(request: Request):
    """Existing streams stop when their session or tenant permissions change."""
    service = getattr(getattr(getattr(request, "app", None), "state", None), "account_identity", None)
    if service is None:
        return
    credential = getattr(request.state, "api_credential", None)
    if credential:
        from app.multitenancy.tokens import Tokens

        account, _ = Tokens(service).authenticate(
            credential, request.state.tenant_context.tenant_id, request.state.request_id,
            request.method, request.url.path, touch=False,
        )
    else:
        account = service.current_user(request.cookies.get(COOKIE_NAME, ""))
    original = request.state.account
    if (account["id"] != original["id"] or account["auth_version"] != original["auth_version"]
            or account.get("default_tenant_id") != original.get("default_tenant_id")):
        raise AccessError(401, "账户权限已变更，请重新登录")
    context = getattr(request.state, "tenant_context", None)
    if context:
        current = service.context(account, context.tenant_id, request.state.request_id)
        if current.policy_version != context.policy_version or current.routing_epoch != context.routing_epoch:
            raise AccessError(403, "租户权限已变更，请重新连接")
