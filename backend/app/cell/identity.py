import secrets
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.cell.context import ROLE_PERMISSIONS, TOKEN_PERMISSIONS, AccessError, Role, TenantContext
from app.cell.database import Databases
from app.cell.passwords import DUMMY_HASH, DUMMY_SALT, normalize_email, token_hash, verify_password


class Identity:
    def __init__(self, db: Databases):
        self.db = db

    def login(self, email: str, password: str) -> dict:
        email = normalize_email(email)
        email_digest = token_hash(email)
        credential = "tsp_cs_" + secrets.token_urlsafe(32)
        denied = False
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute("SELECT set_config('statement_timeout', '10000', true)")
            attempt = conn.execute(
                """INSERT INTO control.login_attempts(email_digest) VALUES (%s)
                   ON CONFLICT (email_digest) DO UPDATE SET email_digest = excluded.email_digest
                   RETURNING *""",
                (email_digest,),
            ).fetchone()
            if (datetime.now(UTC) - attempt["window_started"]).total_seconds() >= 900:
                conn.execute(
                    """UPDATE control.login_attempts SET failures = 0, window_started = now()
                       WHERE email_digest = %s""", (email_digest,),
                )
                attempt["failures"] = 0
            if attempt["failures"] >= 10:
                raise AccessError(429, "login_rate_limited")
            user = conn.execute(
                "SELECT * FROM control.users WHERE email = %s FOR SHARE", (email,)
            ).fetchone()
            valid = verify_password(
                password, (user or {}).get("password_salt", DUMMY_SALT),
                (user or {}).get("password_hash", DUMMY_HASH),
            )
            if not user or not valid or not user["active"]:
                conn.execute(
                    "UPDATE control.login_attempts SET failures = failures + 1 WHERE email_digest = %s",
                    (email_digest,),
                )
                denied = True
            else:
                conn.execute("DELETE FROM control.login_attempts WHERE email_digest = %s", (email_digest,))
                expires = datetime.now(UTC) + timedelta(seconds=self.db.settings.session_ttl_seconds)
                conn.execute(
                    "INSERT INTO control.sessions(digest, user_id, expires_at) VALUES (%s, %s, %s)",
                    (token_hash(credential), user["id"], expires),
                )
        if denied:
            raise AccessError(401, "invalid_credentials")
        return {"token": credential, "expires_at": expires, "user": {"id": user["id"], "email": email}}

    @staticmethod
    def _session(conn, credential: str, *, exclusive: bool = False) -> dict:
        if not credential.startswith("tsp_cs_"):
            raise AccessError(401, "session_required")
        row = conn.execute(
            f"""SELECT s.user_id, u.email FROM control.sessions s
               JOIN control.users u ON u.id = s.user_id
               WHERE s.digest = %s AND s.expires_at > now() AND u.active
               FOR {'UPDATE' if exclusive else 'SHARE'} OF s FOR SHARE OF u""", (token_hash(credential),),
        ).fetchone()
        if not row:
            raise AccessError(401, "invalid_credentials")
        return row

    def logout(self, credential: str) -> None:
        with self.db.control.connection() as conn, conn.transaction():
            self._session(conn, credential, exclusive=True)
            conn.execute("DELETE FROM control.sessions WHERE digest = %s", (token_hash(credential),))

    def directory(self, credential: str) -> list[dict]:
        with self.db.control.connection() as conn, conn.transaction():
            session = self._session(conn, credential)
            return conn.execute(
                """SELECT t.id, t.name, t.cell_id, t.routing_epoch, t.status,
                          t.policy_version, m.role, c.api_url
                   FROM control.memberships m JOIN control.tenants t ON t.id = m.tenant_id
                   JOIN control.cells c ON c.id = t.cell_id
                   WHERE m.user_id = %s ORDER BY t.id""", (session["user_id"],),
            ).fetchall()

    @contextmanager
    def authorized(
        self, credential: str, tenant_id: str, epoch: int, request_id: str,
        permission: str, *, exclusive: bool = False,
    ):
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute("SELECT set_config('statement_timeout', '10000', true)")
            # Lock tenant before token rows: member deletion/revocation uses the same order.
            lock = "UPDATE" if exclusive else "SHARE"
            tenant = conn.execute(
                f"""SELECT t.*, c.status AS cell_status FROM control.tenants t
                    JOIN control.cells c ON c.id = t.cell_id WHERE t.id = %s
                    FOR {lock} OF t FOR SHARE OF c""", (tenant_id,),
            ).fetchone()
            api_token = None
            if credential.startswith("tsp_cs_"):
                user = self._session(conn, credential)
            elif credential.startswith("tsp_ct_"):
                api_token = conn.execute(
                    """SELECT a.*, u.email FROM control.api_tokens a
                       JOIN control.users u ON u.id = a.user_id
                       WHERE a.digest = %s AND a.revoked_at IS NULL
                       AND a.expires_at > now() AND u.active FOR SHARE OF a, u""",
                    (token_hash(credential),),
                ).fetchone()
                if not api_token:
                    raise AccessError(401, "invalid_credentials")
                if api_token["tenant_id"] != tenant_id:
                    raise AccessError(403, "token_tenant_mismatch")
                user = api_token
            else:
                raise AccessError(401, "invalid_credentials")
            member = conn.execute(
                """SELECT role FROM control.memberships WHERE tenant_id = %s AND user_id = %s
                   FOR SHARE""", (tenant_id, user["user_id"]),
            ).fetchone()
            if not tenant or not member:
                raise AccessError(403, "tenant_access_denied")
            if tenant["status"] != "active" or tenant["cell_status"] == "disabled":
                raise AccessError(403, "tenant_unavailable")
            if tenant["cell_id"] != self.db.settings.id:
                raise AccessError(409, "wrong_cell")
            if tenant["routing_epoch"] != epoch:
                raise AccessError(409, "routing_epoch_stale")
            role = Role(member["role"])
            permissions = ROLE_PERMISSIONS[role]
            if api_token:
                if api_token["policy_version"] != tenant["policy_version"]:
                    raise AccessError(401, "token_policy_stale")
                permissions = permissions & frozenset(api_token["permissions"])
            context = TenantContext(
                tenant_id, user["user_id"], tenant["cell_id"], role, permissions,
                tenant["policy_version"], tenant["routing_epoch"], request_id,
                api_token["id"] if api_token else None,
            )
            context.require(permission)
            yield context, conn

    @contextmanager
    def worker_context(self, tenant_id: str, user_id: str | None = None):
        """Called only by the trusted Worker; never reachable through client identity headers."""
        with self.db.control.connection() as conn, conn.transaction():
            tenant = conn.execute(
                """SELECT t.* FROM control.tenants t JOIN control.cells c ON c.id=t.cell_id
                   WHERE t.id=%s AND t.status='active' AND c.status!='disabled' FOR SHARE OF t,c""",
                (tenant_id,),
            ).fetchone()
            if not tenant or tenant["cell_id"] != self.db.settings.id:
                raise AccessError(403, "tenant_unavailable")
            if user_id:
                member = conn.execute(
                    """SELECT m.role FROM control.memberships m JOIN control.users u ON u.id=m.user_id
                       WHERE m.tenant_id=%s AND m.user_id=%s AND u.active FOR SHARE OF m,u""",
                    (tenant_id, user_id),
                ).fetchone()
                if not member:
                    raise AccessError(403, "tenant_access_denied")
                role = Role(member["role"])
                permissions = ROLE_PERMISSIONS[role]
            else:
                role = Role.OWNER
                permissions = frozenset({"alerts:write", "paper:write", "strategies:write"})
            context = TenantContext(
                tenant_id, user_id or "cell-worker", self.db.settings.id, role, permissions,
                tenant["policy_version"], tenant["routing_epoch"], uuid.uuid4().hex,
            )
            yield context, conn

    @staticmethod
    def authorize_background(conn, ctx, row, required: set[str], *, admin=False):
        member = conn.execute(
            """SELECT m.role FROM control.memberships m JOIN control.users u ON u.id=m.user_id
               WHERE m.tenant_id=%s AND m.user_id=%s AND u.active FOR SHARE OF m,u""",
            (ctx.tenant_id, row["user_id"]),
        ).fetchone()
        if not member or not required <= ROLE_PERMISSIONS[Role(member["role"])] or (
            admin and member["role"] not in {"owner", "admin"}
        ) or ctx.policy_version != row["policy_version"] or ctx.routing_epoch != row["routing_epoch"]:
            raise AccessError(403, "job_authorization_changed")
        token_id = row.get("token_id") or row.get("input", {}).get("submitter_token_id")
        if token_id:
            token = conn.execute(
                """SELECT permissions FROM control.api_tokens WHERE id=%s AND tenant_id=%s AND user_id=%s
                   AND revoked_at IS NULL AND expires_at>now() AND policy_version=%s FOR SHARE""",
                (token_id, ctx.tenant_id, row["user_id"], ctx.policy_version),
            ).fetchone()
            if not token or not required <= set(token["permissions"]):
                raise AccessError(403, "job_authorization_changed")

    @staticmethod
    def audit(conn, context: TenantContext, action: str, target: str) -> None:
        conn.execute(
            """INSERT INTO control.audit(tenant_id, actor_id, action, target_id, request_id)
               VALUES (%s, %s, %s, %s, %s)""",
            (context.tenant_id, context.user_id, action, target, context.request_id),
        )

    @staticmethod
    def members(conn, context: TenantContext) -> list[dict]:
        return conn.execute(
            """SELECT m.user_id, u.email, m.role FROM control.memberships m
               JOIN control.users u ON u.id = m.user_id WHERE m.tenant_id = %s ORDER BY u.email""",
            (context.tenant_id,),
        ).fetchall()

    def set_member(self, conn, context: TenantContext, user_id: str, role: Role | None) -> None:
        context.require("members:manage")
        target = conn.execute(
            "SELECT role FROM control.memberships WHERE tenant_id = %s AND user_id = %s",
            (context.tenant_id, user_id),
        ).fetchone()
        if context.role != Role.OWNER and (role == Role.OWNER or (target and target["role"] == "owner")):
            raise AccessError(403, "owner_required")
        if target and target["role"] == "owner" and role != Role.OWNER:
            count = conn.execute(
                "SELECT count(*) AS n FROM control.memberships WHERE tenant_id = %s AND role = 'owner'",
                (context.tenant_id,),
            ).fetchone()["n"]
            if count <= 1:
                raise AccessError(409, "last_owner")
        if role is None:
            # Token FK is intentionally not cascaded: revoke/delete before removing membership.
            conn.execute(
                "DELETE FROM control.api_tokens WHERE tenant_id = %s AND user_id = %s",
                (context.tenant_id, user_id),
            )
            conn.execute(
                "DELETE FROM control.memberships WHERE tenant_id = %s AND user_id = %s",
                (context.tenant_id, user_id),
            )
        else:
            if not conn.execute("SELECT 1 FROM control.users WHERE id = %s AND active", (user_id,)).fetchone():
                raise AccessError(404, "user_not_found")
            conn.execute(
                """INSERT INTO control.memberships(tenant_id, user_id, role) VALUES (%s, %s, %s)
                   ON CONFLICT (tenant_id, user_id) DO UPDATE SET role = excluded.role""",
                (context.tenant_id, user_id, role.value),
            )
        conn.execute(
            "UPDATE control.tenants SET policy_version = policy_version + 1 WHERE id = %s",
            (context.tenant_id,),
        )
        self.audit(conn, context, "member.remove" if role is None else "member.set", user_id)

    def create_token(self, conn, context: TenantContext, name: str, permissions: list[str], days: int) -> dict:
        context.require("tokens:manage")
        requested = frozenset(permissions)
        if not requested or not requested <= TOKEN_PERMISSIONS or not requested <= context.permissions:
            raise AccessError(403, "invalid_token_permissions")
        value = "tsp_ct_" + secrets.token_urlsafe(32)
        token_id = uuid.uuid4().hex
        expires = datetime.now(UTC) + timedelta(days=days)
        conn.execute(
            """INSERT INTO control.api_tokens
               (id, tenant_id, user_id, digest, name, permissions, policy_version, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (token_id, context.tenant_id, context.user_id, token_hash(value), name,
             sorted(requested), context.policy_version, expires),
        )
        self.audit(conn, context, "token.create", token_id)
        return {"id": token_id, "token": value, "expires_at": expires}

    def tokens(self, conn, context: TenantContext) -> list[dict]:
        return conn.execute(
            """SELECT id, name, permissions, expires_at, revoked_at FROM control.api_tokens
               WHERE tenant_id = %s AND user_id = %s ORDER BY created_at DESC""",
            (context.tenant_id, context.user_id),
        ).fetchall()

    def revoke_token(self, conn, context: TenantContext, token_id: str) -> None:
        result = conn.execute(
            """UPDATE control.api_tokens SET revoked_at = now()
               WHERE tenant_id = %s AND id = %s AND user_id = %s""",
            (context.tenant_id, token_id, context.user_id),
        )
        if not result.rowcount:
            raise AccessError(404, "token_not_found")
        self.audit(conn, context, "token.revoke", token_id)
