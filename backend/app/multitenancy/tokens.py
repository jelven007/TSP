"""Expiring full-product credentials, bound to one tenant and current RBAC."""
from __future__ import annotations

import secrets
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.cell.context import READ_PERMISSIONS, WRITE_PERMISSIONS, AccessError
from app.cell.passwords import token_hash
from app.multitenancy.routes import identity, require
from app.services.api_gateway import rate_limit_per_min, required_scope

router = APIRouter(prefix="/api/settings/api-tokens", tags=["API tokens"])
SCOPES = frozenset({"read:market", "read:ext", "read:analysis", "run:backtest", "paper:trade"})


def _view(row):
    return {
        "id": row["id"], "name": row["name"], "tenant_id": row["tenant_id"],
        "scopes": row["full_scopes"], "created_at": row["created_at"],
        "last_used_at": row["last_used_at"], "expires_at": row["expires_at"],
        "revoked": row["revoked_at"] is not None,
    }


class Tokens:
    def __init__(self, service):
        self.identity = service

    def authenticate(self, value, selected, request_id, method, path, *, touch=True):
        if not value.startswith("tsp_ft_") or len(value) > 256:
            raise AccessError(401, "API Token 无效或已吊销")
        with self.identity.db.control.connection() as conn:
            row = conn.execute(
                """SELECT a.* FROM control.api_tokens a JOIN control.users u ON u.id=a.user_id
                   WHERE a.digest=%s AND a.revoked_at IS NULL AND a.expires_at>now()
                   AND u.active AND u.account_state='active' AND a.auth_version=u.auth_version""",
                (token_hash(value),),
            ).fetchone()
            if not row or not row["full_scopes"]:
                raise AccessError(401, "API Token 无效或已吊销")
            user = conn.execute("SELECT * FROM control.users WHERE id=%s", (row["user_id"],)).fetchone()
            if (not user or not user["active"] or user["account_state"] != "active"
                    or user["auth_version"] != row["auth_version"]):
                raise AccessError(401, "API Token 账户授权已变更")
            account = self.identity._public(conn, user)
        if selected and selected != row["tenant_id"]:
            raise AccessError(403, "Token 与目标租户不匹配")
        context = self.identity.context(account, row["tenant_id"], request_id)
        if row["policy_version"] != context.policy_version or row["routing_epoch"] != context.routing_epoch:
            raise AccessError(401, "Token 租户授权已变更")
        scope = required_scope(method, path)
        stream_ticket = method == "POST" and path == "/api/events/ticket"
        if scope == "read:ext" and not (
            path == "/api/ext-data/schema-all" or path.startswith("/api/ext-data/schema/")
            or path.endswith(("/rows", "/values"))
        ):
            scope = None
        if not stream_ticket and (scope not in SCOPES or scope not in row["full_scopes"]):
            raise AccessError(403, "Token 无权访问该接口")
        if touch:
            self.identity.limit("full-token:" + row["id"], rate_limit_per_min(), 60)
            with self.identity.db.control.connection() as conn:
                conn.execute(
                    """UPDATE control.api_tokens SET last_used_at=now() WHERE id=%s
                       AND (last_used_at IS NULL OR last_used_at<now()-interval '60 seconds')""", (row["id"],),
                )
        # Scope is always computed from the credential's tenant, independently
        # of the administrator's current UI selection.
        account = {**account, "default_tenant_id": context.tenant_id}
        permissions = context.permissions & frozenset(row["permissions"])
        return account, replace(
            context,
            token_id=row["id"],
            token_scopes=frozenset(row["full_scopes"]),
            permissions=permissions,
        )

    def create(self, account, name, scopes, days, request_id):
        if "tokens:manage" not in account["permissions"]:
            raise AccessError(403, "无权管理 API Token")
        if not name.strip() or not scopes or not set(scopes) <= SCOPES or not 1 <= days <= 365:
            raise AccessError(400, "Token 权限或有效期无效")
        context = self.identity.context(account, None, request_id)
        value, item_id = "tsp_ft_" + secrets.token_urlsafe(32), uuid.uuid4().hex
        permissions = READ_PERMISSIONS
        if "run:backtest" in scopes:
            permissions |= {"strategies:write"}
        if "paper:trade" in scopes:
            permissions |= {"paper:write"}
        if not permissions <= context.permissions or not permissions <= READ_PERMISSIONS | WRITE_PERMISSIONS:
            raise AccessError(403, "Token 权限超出账户授权")
        from app.multitenancy.research import authorize

        with (authorize(self.identity, context, account["auth_version"]),
              self.identity.db.control.connection() as conn, conn.transaction()):
            # Serialize creation per owner, so concurrent requests honor limits.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("full-token:" + account["id"],))
            if conn.execute(
                """SELECT count(*) AS n FROM control.api_tokens
                   WHERE user_id=%s AND revoked_at IS NULL AND expires_at>now()""", (account["id"],),
            ).fetchone()["n"] >= 50:
                raise AccessError(429, "有效 Token 已达上限")
            row = conn.execute(
                """INSERT INTO control.api_tokens
                   (id,tenant_id,user_id,digest,name,permissions,policy_version,expires_at,
                    full_scopes,auth_version,routing_epoch)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (item_id, context.tenant_id, account["id"], token_hash(value), name.strip(),
                 sorted(permissions), context.policy_version, datetime.now(UTC) + timedelta(days=days),
                 sorted(set(scopes)), account["auth_version"], context.routing_epoch),
            ).fetchone()
            conn.execute(
                """INSERT INTO control.audit(tenant_id,actor_id,action,target_id,request_id)
                   VALUES(%s,%s,'full-token.create',%s,%s)""",
                (context.tenant_id, account["id"], item_id, request_id),
            )
        return {"token": _view(row), "plaintext": value}

    def list(self, account):
        context = self.identity.context(account, None, "token-list")
        with self.identity.db.control.connection() as conn:
            return [_view(row) for row in conn.execute(
                """SELECT * FROM control.api_tokens WHERE tenant_id=%s AND user_id=%s
                   AND cardinality(full_scopes)>0 ORDER BY created_at DESC LIMIT 200""",
                (context.tenant_id, account["id"]),
            ).fetchall()]

    def revoke(self, account, item_id, request_id):
        context = self.identity.context(account, None, request_id)
        with self.identity.db.control.connection() as conn, conn.transaction():
            row = conn.execute(
                """UPDATE control.api_tokens SET revoked_at=now()
                   WHERE id=%s AND tenant_id=%s AND user_id=%s RETURNING id""",
                (item_id, context.tenant_id, account["id"]),
            ).fetchone()
            if not row:
                raise AccessError(404, "Token 不存在")
            conn.execute(
                """INSERT INTO control.audit(tenant_id,actor_id,action,target_id,request_id)
                   VALUES(%s,%s,'full-token.revoke',%s,%s)""",
                (context.tenant_id, account["id"], item_id, request_id),
            )
            from app.multitenancy.stream_auth import notify_access_change

            notify_access_change(conn, principal="api:" + item_id)


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[str] = Field(min_length=1, max_length=5)
    days: int = Field(default=90, ge=1, le=365)


@router.get("")
def list_tokens(request: Request):
    return {"tokens": Tokens(identity(request)).list(require(request, "tokens:manage"))}


@router.post("")
def create_token(body: TokenIn, request: Request):
    return Tokens(identity(request)).create(
        require(request, "tokens:manage"), body.name, body.scopes, body.days, request.state.request_id,
    )


@router.delete("/{token_id}")
def revoke_token(token_id: str, request: Request):
    Tokens(identity(request)).revoke(require(request, "tokens:manage"), token_id, request.state.request_id)
    return {"status": "revoked", "id": token_id}
