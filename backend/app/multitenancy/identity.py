"""PostgreSQL account identity, RBAC, and recoverable personal-tenant creation."""
# ruff: noqa: RUF001 -- Chinese account messages use localized punctuation.
from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from app.cell.context import ROLE_PERMISSIONS, AccessError, Role, TenantContext
from app.cell.database import Databases
from app.cell.passwords import DUMMY_HASH, DUMMY_SALT, hash_password, token_hash, verify_password
from app.services import email_adapter

_PASSWORD_BUDGET = threading.BoundedSemaphore(4)
CODE_TTL = 600
RESEND_SECONDS = 60
# Schema v5 only permits purpose='registration'. Recovery records therefore
# use an email key that normalize_email can never accept, keeping both code
# namespaces isolated without changing an already-deployed schema checksum.
RECOVERY_KEY_PREFIX = "password-recovery "


def _email(value: str) -> str:
    from app.cell.passwords import normalize_email

    try:
        return normalize_email(value)
    except ValueError as exc:
        raise AccessError(400, "请输入有效的邮箱地址") from exc


def _hash_value(value: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt), 200_000).hex()


@contextmanager
def _password_budget():
    if not _PASSWORD_BUDGET.acquire(timeout=5):
        raise AccessError(429, "登录请求繁忙，请稍后重试")
    try:
        yield
    finally:
        _PASSWORD_BUDGET.release()


class AccountIdentity:
    def __init__(self, db: Databases):
        if db.settings.schema_version < 5:
            raise ValueError("full-product identity requires schema v5 or newer")
        self.db = db

    def limit(self, key: str, maximum: int, window: int) -> None:
        """Shared across API processes; count attempts before expensive work."""
        with self.db.control.connection() as conn, conn.transaction():
            row = conn.execute(
                """INSERT INTO control.rate_limits(key,count) VALUES(%s,1)
                   ON CONFLICT(key) DO UPDATE SET
                     count=CASE WHEN control.rate_limits.window_started <= now()-make_interval(secs=>%s)
                         THEN 1 ELSE control.rate_limits.count+1 END,
                     window_started=CASE WHEN control.rate_limits.window_started <= now()-make_interval(secs=>%s)
                         THEN now() ELSE control.rate_limits.window_started END
                   RETURNING count""", (token_hash(key), window, window),
            ).fetchone()
            conn.execute(
                "DELETE FROM control.rate_limits WHERE window_started < now()-interval '1 day'"
            )
        if row["count"] > maximum:
            raise AccessError(429, "请求过于频繁，请稍后重试")

    @staticmethod
    def _public(conn, user: dict) -> dict:
        rows = conn.execute(
            """SELECT ur.role_id,rp.permission_id FROM control.user_roles ur
               LEFT JOIN control.role_permissions rp ON rp.role_id=ur.role_id
               WHERE ur.user_id=%s""", (user["id"],),
        ).fetchall()
        roles = sorted({row["role_id"] for row in rows})
        if not roles:
            raise AccessError(403, "账户未分配系统角色")
        return {
            "id": user["id"], "email": user["email"], "username": user["username"],
            "created_at": int(user["created_at"].timestamp()),
            "role": "admin" if "admin" in roles else "tenant",
            "permissions": sorted({row["permission_id"] for row in rows if row["permission_id"]}),
            "default_tenant_id": user["default_tenant_id"], "auth_version": user["auth_version"],
        }

    def current_user(self, token: str) -> dict:
        if not token or len(token) > 256:
            raise AccessError(401, "未登录或会话已过期")
        with self.db.control.connection() as conn:
            row = conn.execute(
                """SELECT u.* FROM control.sessions s JOIN control.users u ON u.id=s.user_id
                   WHERE s.digest=%s AND s.expires_at>now() AND u.active
                   AND u.account_state='active' AND u.auth_version=s.auth_version""",
                (token_hash(token),),
            ).fetchone()
            if not row:
                raise AccessError(401, "未登录或会话已过期")
            return self._public(conn, row)

    def _session(self, user_id: str, expected_version: int | None = None) -> tuple[str, dict]:
        token = secrets.token_urlsafe(32)
        with self.db.control.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT * FROM control.users WHERE id=%s FOR SHARE", (user_id,),
            ).fetchone()
            if not row or not row["active"] or row["account_state"] != "active":
                raise AccessError(403, "账户不可用")
            if expected_version is not None and row["auth_version"] != expected_version:
                raise AccessError(401, "账户权限已变更，请重新登录")
            conn.execute(
                """INSERT INTO control.sessions(digest,user_id,expires_at,auth_version)
                   VALUES(%s,%s,%s,%s)""",
                (token_hash(token), user_id,
                 datetime.now(UTC) + timedelta(seconds=self.db.settings.session_ttl_seconds),
                 row["auth_version"]),
            )
            conn.execute("DELETE FROM control.sessions WHERE expires_at<=now()")
            return token, self._public(conn, row)

    def login(self, identifier: str, password: str, ip: str) -> tuple[str, dict]:
        identifier = str(identifier or "").strip().casefold()
        if not identifier or len(identifier) > 254 or not 1 <= len(password) <= 128:
            raise AccessError(400, "请输入用户名/邮箱和密码")
        self.limit("login:ip:" + ip, 300, 60)
        self.limit("login:account:" + identifier, 30, 900)
        self.limit("login:global", 1000, 60)
        with self.db.control.connection() as conn:
            user = conn.execute(
                "SELECT * FROM control.users WHERE email=%s OR username=%s", (identifier, identifier),
            ).fetchone()
        with _password_budget():
            valid = verify_password(
                password, (user or {}).get("password_salt", DUMMY_SALT),
                (user or {}).get("password_hash", DUMMY_HASH),
            )
        if not user or not valid or user["account_state"] == "suspended":
            raise AccessError(401, "用户名/邮箱或密码错误")
        if user["account_state"] == "pending":
            self.provision(user["id"])
        elif not user["active"]:
            raise AccessError(401, "用户名/邮箱或密码错误")
        return self._session(user["id"], user["auth_version"])

    def logout(self, token: str) -> None:
        digest = token_hash(token)
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM control.sessions WHERE digest=%s", (digest,))
            from app.multitenancy.stream_auth import notify_access_change

            notify_access_change(conn, principal="session:" + digest)

    def change_password(self, token: str, old_password: str, new_password: str) -> None:
        user = self.current_user(token)
        if not 8 <= len(new_password) <= 128:
            raise AccessError(400, "新密码长度必须在 8 到 128 位之间")
        with self.db.control.connection() as conn:
            record = conn.execute(
                "SELECT password_salt,password_hash FROM control.users WHERE id=%s", (user["id"],),
            ).fetchone()
        with _password_budget():
            if not verify_password(old_password, record["password_salt"], record["password_hash"]):
                raise AccessError(400, "旧密码错误")
            salt, digest = hash_password(new_password)
        with self.db.control.connection() as conn, conn.transaction():
            changed = conn.execute(
                """UPDATE control.users SET password_salt=%s,password_hash=%s,auth_version=auth_version+1
                   WHERE id=%s AND auth_version=%s""",
                (salt, digest, user["id"], user["auth_version"]),
            ).rowcount
            if not changed:
                raise AccessError(409, "账户已变更，请重新登录")
            conn.execute("DELETE FROM control.sessions WHERE user_id=%s", (user["id"],))
            conn.execute(
                "UPDATE control.api_tokens SET revoked_at=now() WHERE user_id=%s", (user["id"],),
            )
            from app.multitenancy.stream_auth import notify_access_change

            notify_access_change(conn, user_id=user["id"])

    def registration_enabled(self) -> bool:
        with self.db.control.connection() as conn:
            return bool(conn.execute("SELECT 1 FROM control.registration_config").fetchone())

    def set_registration_secret(self, value: str, *, only_if_missing: bool = False) -> None:
        value = str(value or "").strip()
        if not 1 <= len(value) <= 128:
            raise AccessError(400, "注册口令长度必须在 1 到 128 位之间")
        salt = secrets.token_hex(16)
        with _password_budget():
            digest = _hash_value(value, salt)
        with self.db.control.connection() as conn:
            if only_if_missing:
                conn.execute(
                    """INSERT INTO control.registration_config(secret_salt,secret_hash)
                       VALUES(%s,%s) ON CONFLICT DO NOTHING""", (salt, digest),
                )
            else:
                conn.execute(
                    """INSERT INTO control.registration_config(secret_salt,secret_hash) VALUES(%s,%s)
                       ON CONFLICT(singleton) DO UPDATE SET secret_salt=excluded.secret_salt,
                         secret_hash=excluded.secret_hash,version=control.registration_config.version+1,
                         updated_at=now()""", (salt, digest),
                )

    def send_registration_code(self, email: str, secret: str, ip: str) -> int:
        email = _email(email)
        self.limit("register:ip:" + ip, 10, 3600)
        self.limit("register:global", 300, 3600)
        with self.db.control.connection() as conn:
            config = conn.execute("SELECT * FROM control.registration_config").fetchone()
            existing = conn.execute("SELECT 1 FROM control.users WHERE email=%s", (email,)).fetchone()
        if not config:
            raise AccessError(503, "注册口令尚未配置")
        with _password_budget():
            valid = verify_password(secret, config["secret_salt"], config["secret_hash"])
        if not valid:
            raise AccessError(403, "注册口令错误")
        if existing:
            raise AccessError(409, "该邮箱已注册")
        smtp, password = email_adapter.get_registration_smtp(email)
        if not email_adapter.is_configured(smtp):
            raise AccessError(503, "注册邮件服务尚未配置")
        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        with _password_budget():
            digest = _hash_value(code, salt)
        now = datetime.now(UTC)
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registration:" + email,))
            old = conn.execute(
                "SELECT resend_at FROM control.verification_codes WHERE email=%s AND purpose='registration'",
                (email,),
            ).fetchone()
            if old and old["resend_at"] > now:
                raise AccessError(429, f"请 {max(1, int((old['resend_at']-now).total_seconds()))} 秒后重新发送")
            conn.execute(
                """INSERT INTO control.verification_codes
                   (email,purpose,salt,code_hash,secret_version,expires_at,resend_at,attempts_left)
                   VALUES(%s,'registration',%s,%s,%s,%s,%s,5)
                   ON CONFLICT(email,purpose) DO UPDATE SET salt=excluded.salt,code_hash=excluded.code_hash,
                     secret_version=excluded.secret_version,expires_at=excluded.expires_at,
                     resend_at=excluded.resend_at,attempts_left=5,delivered=false,consumed_at=NULL""",
                (email, salt, digest, config["version"],
                 now + timedelta(seconds=CODE_TTL), now + timedelta(seconds=RESEND_SECONDS)),
            )
        sent = email_adapter.send_email(
            smtp, password, "TSP 注册验证码",
            f"你的注册验证码是: {code}\n\n验证码将在 10 分钟后失效，请勿转发给他人。\n"
            "如果不是你本人操作，请忽略此邮件。", max_attempts=1,
        )
        with self.db.control.connection() as conn:
            if sent:
                conn.execute(
                    """UPDATE control.verification_codes SET delivered=true
                       WHERE email=%s AND purpose='registration' AND code_hash=%s""", (email, digest),
                )
            else:
                conn.execute(
                    """DELETE FROM control.verification_codes
                       WHERE email=%s AND purpose='registration' AND code_hash=%s""", (email, digest),
                )
        if not sent:
            raise AccessError(503, "验证码邮件发送失败，请稍后重试")
        return RESEND_SECONDS

    def send_recovery_code(self, email: str, secret: str, ip: str) -> int:
        email = _email(email)
        self.limit("recovery:ip:" + ip, 10, 3600)
        self.limit("recovery:account:" + email, 5, 3600)
        self.limit("recovery:global", 300, 3600)
        with self.db.control.connection() as conn:
            config = conn.execute("SELECT * FROM control.registration_config").fetchone()
            existing = conn.execute(
                "SELECT 1 FROM control.users WHERE email=%s", (email,),
            ).fetchone()
        if not config:
            raise AccessError(503, "注册口令尚未配置")
        with _password_budget():
            valid = verify_password(secret, config["secret_salt"], config["secret_hash"])
        if not valid:
            raise AccessError(403, "注册口令错误")
        smtp, password = email_adapter.get_registration_smtp(email)
        if not email_adapter.is_configured(smtp):
            raise AccessError(503, "密码找回邮件服务尚未配置")

        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = secrets.token_hex(16)
        with _password_budget():
            digest = _hash_value(code, salt)
        now = datetime.now(UTC)
        verification_email = RECOVERY_KEY_PREFIX + email
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("password-recovery:" + email,),
            )
            old = conn.execute(
                """SELECT resend_at FROM control.verification_codes
                   WHERE email=%s AND purpose='registration'""",
                (verification_email,),
            ).fetchone()
            if old and old["resend_at"] > now:
                wait = max(1, int((old["resend_at"] - now).total_seconds()))
                raise AccessError(429, f"请 {wait} 秒后重新发送")
            conn.execute(
                """INSERT INTO control.verification_codes
                   (email,purpose,salt,code_hash,secret_version,expires_at,resend_at,attempts_left)
                   VALUES(%s,'registration',%s,%s,%s,%s,%s,5)
                   ON CONFLICT(email,purpose) DO UPDATE SET salt=excluded.salt,
                     code_hash=excluded.code_hash,secret_version=excluded.secret_version,
                     expires_at=excluded.expires_at,resend_at=excluded.resend_at,
                     attempts_left=5,delivered=false,consumed_at=NULL""",
                (
                    verification_email,
                    salt,
                    digest,
                    config["version"],
                    now + timedelta(seconds=CODE_TTL),
                    now + timedelta(seconds=RESEND_SECONDS),
                ),
            )
        if not existing:
            return RESEND_SECONDS

        sent = email_adapter.send_email(
            smtp,
            password,
            "TSP 密码找回验证码",
            f"你的密码找回验证码是: {code}\n\n验证码将在 10 分钟后失效，请勿转发给他人。\n"
            "如果不是你本人操作，请忽略此邮件。",
            max_attempts=1,
        )
        with self.db.control.connection() as conn:
            if sent:
                conn.execute(
                    """UPDATE control.verification_codes SET delivered=true
                       WHERE email=%s AND purpose='registration' AND code_hash=%s""",
                    (verification_email, digest),
                )
            else:
                conn.execute(
                    """DELETE FROM control.verification_codes
                       WHERE email=%s AND purpose='registration' AND code_hash=%s""",
                    (verification_email, digest),
                )
        if not sent:
            raise AccessError(503, "验证码邮件发送失败，请稍后重试")
        return RESEND_SECONDS

    def reset_password(
        self,
        email: str,
        secret: str,
        code: str,
        new_password: str,
    ) -> None:
        email = _email(email)
        if not 8 <= len(new_password) <= 128:
            raise AccessError(400, "新密码长度必须在 8 到 128 位之间")
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise AccessError(400, "请输入六位邮箱验证码")
        with self.db.control.connection() as conn:
            config = conn.execute("SELECT * FROM control.registration_config").fetchone()
        if not config:
            raise AccessError(503, "注册口令尚未配置")
        with _password_budget():
            valid_secret = verify_password(
                secret,
                config["secret_salt"],
                config["secret_hash"],
            )
            salt, digest = hash_password(new_password)
        if not valid_secret:
            raise AccessError(403, "注册口令错误")

        verification_email = RECOVERY_KEY_PREFIX + email
        error = None
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("password-recovery:" + email,),
            )
            verification = conn.execute(
                """SELECT v.*,c.version AS current_version
                   FROM control.verification_codes v
                   JOIN control.registration_config c ON c.singleton
                   WHERE v.email=%s AND v.purpose='registration' FOR UPDATE OF v""",
                (verification_email,),
            ).fetchone()
            if not verification or not verification["delivered"] or verification["consumed_at"]:
                error = "请先发送验证码"
            elif verification["expires_at"] <= datetime.now(UTC):
                error = "验证码已过期，请重新发送"
            elif verification["attempts_left"] <= 0:
                error = "验证码错误次数过多，请重新发送"
            elif verification["secret_version"] != verification["current_version"]:
                error = "注册口令已更新，请重新发送验证码"
            else:
                with _password_budget():
                    valid_code = verify_password(
                        code,
                        verification["salt"],
                        verification["code_hash"],
                    )
                if not valid_code:
                    conn.execute(
                        """UPDATE control.verification_codes SET attempts_left=attempts_left-1
                           WHERE email=%s AND purpose='registration'""",
                        (verification_email,),
                    )
                    error = "验证码错误，请重试"
            user = None
            if not error:
                user = conn.execute(
                    "SELECT id FROM control.users WHERE email=%s FOR UPDATE",
                    (email,),
                ).fetchone()
                if not user:
                    error = "验证码无效，请重新获取"
            if not error:
                conn.execute(
                    """UPDATE control.users
                       SET password_salt=%s,password_hash=%s,auth_version=auth_version+1
                       WHERE id=%s""",
                    (salt, digest, user["id"]),
                )
                conn.execute("DELETE FROM control.sessions WHERE user_id=%s", (user["id"],))
                conn.execute(
                    "UPDATE control.api_tokens SET revoked_at=now() WHERE user_id=%s",
                    (user["id"],),
                )
                from app.multitenancy.stream_auth import notify_access_change

                notify_access_change(conn, user_id=user["id"])
                conn.execute(
                    """UPDATE control.verification_codes SET consumed_at=now()
                       WHERE email=%s AND purpose='registration'""",
                    (verification_email,),
                )
        if error:
            raise AccessError(400, error)

    def register(self, email: str, password: str, code: str) -> tuple[str, dict]:
        email = _email(email)
        if not 8 <= len(password) <= 128:
            raise AccessError(400, "密码长度必须在 8 到 128 位之间")
        if len(code) != 6 or not code.isascii() or not code.isdigit():
            raise AccessError(400, "请输入六位邮箱验证码")
        with _password_budget():
            salt, digest = hash_password(password)
        user_id, tenant_id = uuid.uuid4().hex, uuid.uuid4().hex
        error = None
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("registration:" + email,))
            if conn.execute("SELECT 1 FROM control.users WHERE email=%s", (email,)).fetchone():
                raise AccessError(409, "该邮箱已注册；初始化中断时可通过登录继续")
            verification = conn.execute(
                """SELECT v.*,c.version AS current_version FROM control.verification_codes v
                   JOIN control.registration_config c ON c.singleton
                   WHERE email=%s AND purpose='registration' FOR UPDATE OF v""", (email,),
            ).fetchone()
            if not verification or not verification["delivered"] or verification["consumed_at"]:
                error = "请先发送验证码"
            elif verification["expires_at"] <= datetime.now(UTC):
                error = "验证码已过期，请重新发送"
            elif verification["attempts_left"] <= 0:
                error = "验证码错误次数过多，请重新发送"
            elif verification["secret_version"] != verification["current_version"]:
                error = "注册口令已更新，请重新发送验证码"
            else:
                with _password_budget():
                    valid = verify_password(code, verification["salt"], verification["code_hash"])
                if not valid:
                    conn.execute(
                        """UPDATE control.verification_codes SET attempts_left=attempts_left-1
                           WHERE email=%s AND purpose='registration'""", (email,),
                    )
                    error = "验证码错误，请重试"
            if not error:
                cell = conn.execute(
                    "SELECT * FROM control.cells WHERE id=%s AND status='active' FOR UPDATE",
                    (self.db.settings.id,),
                ).fetchone()
                count = conn.execute(
                    "SELECT count(*) AS n FROM control.tenants WHERE cell_id=%s", (self.db.settings.id,),
                ).fetchone()["n"]
                if not cell or count >= cell["tenant_limit"]:
                    raise AccessError(503, "租户容量暂时不足，请稍后重试")
                conn.execute(
                    """INSERT INTO control.users
                       (id,email,password_salt,password_hash,active,account_state)
                       VALUES(%s,%s,%s,%s,false,'pending')""", (user_id, email, salt, digest),
                )
                conn.execute("INSERT INTO control.user_roles VALUES(%s,'tenant')", (user_id,))
                conn.execute(
                    """INSERT INTO control.tenants(id,name,cell_id,status)
                       VALUES(%s,%s,%s,'moving')""", (tenant_id, email.split("@")[0] + "的工作空间", self.db.settings.id),
                )
                conn.execute("INSERT INTO control.memberships VALUES(%s,%s,'owner')", (tenant_id, user_id))
                conn.execute(
                    "INSERT INTO control.provisioning_jobs(id,user_id,tenant_id) VALUES(%s,%s,%s)",
                    (uuid.uuid4().hex, user_id, tenant_id),
                )
                conn.execute(
                    """UPDATE control.verification_codes SET consumed_at=now()
                       WHERE email=%s AND purpose='registration'""", (email,),
                )
        if error:
            raise AccessError(400, error)
        self.provision(user_id)
        return self._session(user_id)

    def provision(self, user_id: str) -> None:
        """Independent commits; any failure leaves the same durable job to retry."""
        with self.db.control.connection() as conn:
            job = conn.execute(
                """SELECT j.*,t.cell_id,t.routing_epoch,t.status FROM control.provisioning_jobs j
                   JOIN control.tenants t ON t.id=j.tenant_id WHERE j.user_id=%s""", (user_id,),
            ).fetchone()
        if not job or job["cell_id"] != self.db.settings.id:
            raise AccessError(409, "租户初始化记录不可用")
        if job["state"] == "ready":
            return
        if job["status"] != "moving" or job["routing_epoch"] != 1:
            raise AccessError(409, "租户初始化状态已变更")
        try:
            with self.db.data.connection() as conn, conn.transaction():
                conn.execute(
                    "SELECT business.initialize_tenant(%s,%s)", (job["tenant_id"], job["routing_epoch"]),
                )
            with self.db.control.connection() as conn, conn.transaction():
                conn.execute(
                    "SELECT id FROM control.provisioning_jobs WHERE id=%s FOR UPDATE", (job["id"],),
                )
                conn.execute(
                    """UPDATE control.tenants SET status='active'
                       WHERE id=%s AND status='moving' AND routing_epoch=1""", (job["tenant_id"],),
                )
                tenant = conn.execute(
                    "SELECT status FROM control.tenants WHERE id=%s FOR SHARE", (job["tenant_id"],),
                ).fetchone()
                if tenant["status"] != "active":
                    raise AccessError(409, "租户不可用")
                conn.execute(
                    """UPDATE control.users SET active=true,account_state='active',default_tenant_id=%s
                       WHERE id=%s AND account_state='pending'""", (job["tenant_id"], user_id),
                )
                conn.execute(
                    """UPDATE control.provisioning_jobs SET state='ready',attempts=attempts+1,
                       last_error=NULL,updated_at=now() WHERE id=%s""", (job["id"],),
                )
        except Exception as exc:
            with self.db.control.connection() as conn:
                conn.execute(
                    """UPDATE control.provisioning_jobs SET attempts=attempts+1,last_error=%s,updated_at=now()
                       WHERE id=%s AND state='pending'""", (type(exc).__name__, job["id"]),
                )
            if isinstance(exc, AccessError):
                raise
            raise AccessError(503, "账户已保存，租户初始化暂未完成；稍后登录可继续") from exc

    def recover_pending(self, limit: int = 20) -> int:
        with self.db.control.connection() as conn:
            jobs = conn.execute(
                """SELECT j.user_id FROM control.provisioning_jobs j JOIN control.tenants t ON t.id=j.tenant_id
                   WHERE j.state='pending' AND t.cell_id=%s ORDER BY j.updated_at LIMIT %s""",
                (self.db.settings.id, limit),
            ).fetchall()
        recovered = 0
        for job in jobs:
            try:
                self.provision(job["user_id"])
                recovered += 1
            except AccessError:
                continue
        return recovered

    def directory(self, user: dict) -> list[dict]:
        with self.db.control.connection() as conn:
            if user["role"] == "admin":
                return conn.execute(
                    """SELECT t.id,t.name,t.status,t.cell_id,t.routing_epoch,
                              coalesce(owner.account_name,t.name) AS display_name
                       FROM control.tenants t
                       LEFT JOIN LATERAL (
                           SELECT coalesce(u.username,u.email) AS account_name
                           FROM control.memberships m JOIN control.users u ON u.id=m.user_id
                           WHERE m.tenant_id=t.id AND m.role='owner'
                           ORDER BY u.created_at,u.id LIMIT 1
                       ) owner ON true
                       ORDER BY t.created_at,t.id"""
                ).fetchall()
            return conn.execute(
                """SELECT t.id,t.name,t.status,t.cell_id,t.routing_epoch FROM control.tenants t
                   JOIN control.memberships m ON m.tenant_id=t.id WHERE m.user_id=%s ORDER BY t.created_at,t.id""",
                (user["id"],),
            ).fetchall()

    def context(self, user: dict, tenant_id: str | None, request_id: str) -> TenantContext:
        tenant_id = tenant_id or user["default_tenant_id"]
        if not tenant_id:
            raise AccessError(409, "请选择目标租户")
        with self.db.control.connection() as conn:
            row = conn.execute(
                """SELECT t.*,m.role FROM control.tenants t LEFT JOIN control.memberships m
                   ON m.tenant_id=t.id AND m.user_id=%s WHERE t.id=%s""", (user["id"], tenant_id),
            ).fetchone()
            if not row or (not row["role"] and user["role"] != "admin"):
                raise AccessError(403, "无权访问该租户")
            if row["status"] != "active":
                raise AccessError(403, "租户不可用")
            if row["cell_id"] != self.db.settings.id:
                raise AccessError(409, "租户位于其他服务节点")
            role = Role.OWNER if user["role"] == "admin" else Role(row["role"])
            permissions = ROLE_PERMISSIONS[role]
            # Membership only narrows business access; it never widens system RBAC.
            if "business:write" not in user["permissions"]:
                permissions = permissions - {"watchlist:write", "strategies:write", "alerts:write", "paper:write"}
            return TenantContext(
                tenant_id, user["id"], row["cell_id"], role, permissions,
                row["policy_version"], row["routing_epoch"], request_id,
            )

    def choose_tenant(self, user: dict, tenant_id: str, request_id: str) -> None:
        with self.db.control.connection() as conn, conn.transaction():
            current = conn.execute("SELECT * FROM control.users WHERE id=%s FOR UPDATE", (user["id"],)).fetchone()
            if (not current or not current["active"] or current["account_state"] != "active"
                    or current["auth_version"] != user["auth_version"]):
                raise AccessError(401, "账户授权已变更")
            user = self._public(conn, current)
            ctx = self.context(user, tenant_id, request_id)
            conn.execute(
                "UPDATE control.users SET default_tenant_id=%s WHERE id=%s", (tenant_id, user["id"]),
            )
            from app.multitenancy.stream_auth import notify_access_change

            notify_access_change(conn, user_id=user["id"])
            if user["role"] == "admin":
                conn.execute(
                    """INSERT INTO control.audit(tenant_id,actor_id,action,target_id,request_id)
                       VALUES(%s,%s,'admin.tenant.select',%s,%s)""",
                    (ctx.tenant_id, user["id"], tenant_id, request_id),
                )

    def users(self) -> list[dict]:
        with self.db.control.connection() as conn:
            return conn.execute(
                """SELECT u.id,u.email,u.username,u.active,u.account_state,u.created_at,u.default_tenant_id,
                          coalesce(array_agg(ur.role_id) FILTER(WHERE ur.role_id IS NOT NULL),'{}') AS roles
                   FROM control.users u LEFT JOIN control.user_roles ur ON ur.user_id=u.id
                   GROUP BY u.id ORDER BY u.created_at,u.id"""
            ).fetchall()

    def update_user(self, actor: dict, user_id: str, role: str, active: bool, request_id: str) -> None:
        if actor["role"] != "admin":
            raise AccessError(403, "需要管理员权限")
        if role not in {"admin", "tenant"}:
            raise AccessError(400, "不支持的系统角色")
        with self.db.control.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('tsp-admin-role-update'))")
            current_actor = conn.execute(
                "SELECT * FROM control.users WHERE id=%s FOR SHARE", (actor["id"],),
            ).fetchone()
            if (not current_actor or not current_actor["active"] or current_actor["account_state"] != "active"
                    or current_actor["auth_version"] != actor["auth_version"]
                    or self._public(conn, current_actor)["role"] != "admin"):
                raise AccessError(403, "管理员授权已变更")
            target = conn.execute(
                "SELECT * FROM control.users WHERE id=%s FOR UPDATE", (user_id,),
            ).fetchone()
            if not target or target["account_state"] == "pending":
                raise AccessError(404, "账户不存在或初始化未完成")
            is_admin = bool(conn.execute(
                "SELECT 1 FROM control.user_roles WHERE user_id=%s AND role_id='admin'", (user_id,),
            ).fetchone())
            if is_admin and (role != "admin" or not active):
                others = conn.execute(
                    """SELECT count(*) AS n FROM control.user_roles ur JOIN control.users u ON u.id=ur.user_id
                       WHERE ur.role_id='admin' AND u.active AND u.id<>%s""", (user_id,),
                ).fetchone()["n"]
                if not others:
                    raise AccessError(409, "必须保留至少一个有效管理员")
            conn.execute("DELETE FROM control.user_roles WHERE user_id=%s", (user_id,))
            conn.execute("INSERT INTO control.user_roles VALUES(%s,%s)", (user_id, role))
            conn.execute(
                """UPDATE control.users SET active=%s,account_state=%s,auth_version=auth_version+1 WHERE id=%s""",
                (active, "active" if active else "suspended", user_id),
            )
            conn.execute("DELETE FROM control.sessions WHERE user_id=%s", (user_id,))
            conn.execute("UPDATE control.api_tokens SET revoked_at=now() WHERE user_id=%s", (user_id,))
            from app.multitenancy.stream_auth import notify_access_change

            notify_access_change(conn, user_id=user_id)
            conn.execute(
                """INSERT INTO control.audit(tenant_id,actor_id,action,target_id,request_id)
                   VALUES(%s,%s,'user.role_status.set',%s,%s)""",
                (target["default_tenant_id"] or "system", actor["id"], user_id, request_id),
            )
