"""Cookie-authenticated account and system-administration contracts."""
# ruff: noqa: RUF001 -- Chinese account messages use localized punctuation.
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.api.auth import COOKIE_NAME, _client_ip
from app.cell.context import AccessError
from app.multitenancy.identity import AccountIdentity

router = APIRouter(tags=["accounts"])


def identity(request: Request) -> AccountIdentity:
    return request.app.state.account_identity


def user(request: Request) -> dict:
    return getattr(request.state, "account", None) or identity(request).current_user(
        request.cookies.get(COOKIE_NAME, ""),
    )


def require(request: Request, permission: str) -> dict:
    account = user(request)
    if permission not in account["permissions"]:
        raise AccessError(403, "无权使用该功能")
    return account


def _cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=True, samesite="lax", path="/",
        max_age=identity(request).db.settings.session_ttl_seconds,
    )


class LoginIn(BaseModel):
    identifier: str | None = Field(default=None, max_length=254)
    email: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class CodeIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    registration_secret: str = Field(min_length=1, max_length=128)


class RegisterIn(BaseModel):
    # Registration never accepts a role, membership, cell or tenant identifier.
    model_config = {"extra": "forbid"}
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    code: str = Field(pattern=r"^[0-9]{6}$")


class RecoveryResetIn(CodeIn):
    code: str = Field(pattern=r"^[0-9]{6}$")
    new_password: str = Field(min_length=8, max_length=128)


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class TenantIn(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=128)


class RoleIn(BaseModel):
    role: str = Field(pattern=r"^(admin|tenant)$")
    active: bool


class SecretIn(BaseModel):
    registration_secret: str = Field(min_length=1, max_length=128)


@router.get("/api/auth/status")
def status(request: Request) -> dict:
    try:
        account = user(request)
    except AccessError as exc:
        if exc.status != 401:
            raise
        account = None
    return {
        "auth_required": True, "configured": True, "has_users": True,
        "legacy_migration_required": False, "registration_enabled": identity(request).registration_enabled(),
        "email_verification_required": True, "authenticated": account is not None,
        "user": account, "multitenancy_enabled": True,
    }


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    token, account = identity(request).login(
        body.identifier or body.email or "", body.password, _client_ip(request),
    )
    _cookie(response, request, token)
    return {"ok": True, "authenticated": True, "legacy_migration_required": False, "user": account}


@router.post("/api/auth/register/code")
def send_code(body: CodeIn, request: Request) -> dict:
    cooldown = identity(request).send_registration_code(
        body.email, body.registration_secret, _client_ip(request),
    )
    return {"ok": True, "cooldown_seconds": cooldown}


@router.post("/api/auth/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response) -> dict:
    token, account = identity(request).register(body.email, body.password, body.code)
    _cookie(response, request, token)
    return {"ok": True, "authenticated": True, "user": account, "redirect": "/"}


@router.post("/api/auth/recovery/code")
def send_recovery_code(body: CodeIn, request: Request) -> dict:
    cooldown = identity(request).send_recovery_code(
        body.email,
        body.registration_secret,
        _client_ip(request),
    )
    return {
        "ok": True,
        "cooldown_seconds": cooldown,
        "message": "若该邮箱已注册，验证码将发送至邮箱",
    }


@router.post("/api/auth/recovery/reset")
def reset_password(body: RecoveryResetIn, request: Request) -> dict:
    identity(request).limit("recovery-reset:ip:" + _client_ip(request), 30, 3600)
    identity(request).reset_password(
        body.email,
        body.registration_secret,
        body.code,
        body.new_password,
    )
    return {"ok": True, "message": "密码已重置，请使用新密码登录"}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response) -> dict:
    identity(request).logout(request.cookies.get(COOKIE_NAME, ""))
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True}


@router.post("/api/auth/change-password")
def change_password(body: PasswordIn, request: Request, response: Response) -> dict:
    identity(request).limit("password:" + user(request)["id"], 10, 900)
    identity(request).change_password(
        request.cookies.get(COOKIE_NAME, ""), body.old_password, body.new_password,
    )
    response.delete_cookie(COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    return {"ok": True, "message": "密码已修改，请重新登录"}


@router.get("/api/account/tenants")
def tenants(request: Request) -> dict:
    return {"tenants": identity(request).directory(user(request))}


@router.post("/api/account/tenant")
def select_tenant(body: TenantIn, request: Request) -> dict:
    identity(request).choose_tenant(user(request), body.tenant_id, request.state.request_id)
    return {"ok": True}


@router.get("/api/admin/users")
def users(request: Request) -> dict:
    require(request, "users:manage")
    return {"users": identity(request).users()}


@router.put("/api/admin/users/{user_id}")
def update_user(user_id: str, body: RoleIn, request: Request) -> dict:
    actor = require(request, "users:manage")
    identity(request).update_user(actor, user_id, body.role, body.active, request.state.request_id)
    return {"ok": True}


@router.put("/api/admin/registration-secret")
def registration_secret(body: SecretIn, request: Request) -> dict:
    require(request, "users:manage")
    identity(request).set_registration_secret(body.registration_secret)
    return {"ok": True}
