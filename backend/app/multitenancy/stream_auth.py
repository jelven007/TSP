"""Signed tenant stream tickets and cross-process access revocation."""
from __future__ import annotations

import base64
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from pydantic import SecretStr

from app.cell.context import AccessError, Role, TenantContext

logger = logging.getLogger(__name__)
TICKET_PREFIX = "tss_"
ACCESS_CHANNEL = "tsp_access_revoked"


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(frozen=True, slots=True)
class StreamClaims:
    audience: str
    ticket_id: str
    principal: str
    user_id: str
    tenant_id: str
    cell_id: str
    role: str
    permissions: tuple[str, ...]
    token_id: str | None
    token_scopes: tuple[str, ...]
    auth_version: int
    policy_version: int
    routing_epoch: int
    issued_at: float
    expires_at: float

    def context(self, request_id: str) -> TenantContext:
        return TenantContext(
            tenant_id=self.tenant_id,
            user_id=self.user_id,
            cell_id=self.cell_id,
            role=Role(self.role),
            permissions=frozenset(self.permissions),
            policy_version=self.policy_version,
            routing_epoch=self.routing_epoch,
            request_id=request_id,
            token_id=self.token_id,
            token_scopes=frozenset(self.token_scopes) if self.token_id else None,
        )


class StreamTicketCodec:
    """HMAC tickets are verified locally; replay state belongs to the gateway."""

    def __init__(
        self,
        secret: SecretStr | str,
        *,
        audience: str = "tsp-tenant-events",
        ttl_seconds: int = 60,
        max_consumed: int = 20_000,
    ):
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else str(secret)
        if len(value.encode()) < 32:
            raise ValueError("stream ticket secret must contain at least 32 bytes")
        if not 1 <= ttl_seconds <= 60:
            raise ValueError("stream ticket TTL must be in [1, 60] seconds")
        self.secret = value.encode()
        self.audience = audience
        self.ttl_seconds = ttl_seconds
        self.max_consumed = max_consumed
        self._consumed: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, account: dict, context: TenantContext, *, principal: str) -> tuple[str, int]:
        now = time.time()
        payload = {
            "aud": self.audience,
            "jti": secrets.token_urlsafe(18),
            "prn": principal,
            "sub": account["id"],
            "tid": context.tenant_id,
            "cid": context.cell_id,
            "role": context.role.value,
            "perms": sorted(context.permissions),
            "tok": context.token_id,
            "scopes": sorted(context.token_scopes or ()),
            "av": int(account["auth_version"]),
            "pv": int(context.policy_version),
            "re": int(context.routing_epoch),
            "iat": now,
            "exp": now + self.ttl_seconds,
        }
        body = _encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
        signature = _encode(hmac.digest(self.secret, body.encode(), "sha256"))
        return f"{TICKET_PREFIX}{body}.{signature}", self.ttl_seconds

    def consume(self, ticket: str) -> StreamClaims | None:
        if not ticket.startswith(TICKET_PREFIX) or len(ticket) > 4096:
            return None
        try:
            body, provided = ticket[len(TICKET_PREFIX):].split(".", 1)
            expected = _encode(hmac.digest(self.secret, body.encode(), "sha256"))
            if not hmac.compare_digest(provided, expected):
                return None
            value = json.loads(_decode(body))
            claims = StreamClaims(
                audience=str(value["aud"]),
                ticket_id=str(value["jti"]),
                principal=str(value["prn"]),
                user_id=str(value["sub"]),
                tenant_id=str(value["tid"]),
                cell_id=str(value["cid"]),
                role=str(value["role"]),
                permissions=tuple(str(item) for item in value["perms"]),
                token_id=str(value["tok"]) if value["tok"] else None,
                token_scopes=tuple(str(item) for item in value["scopes"]),
                auth_version=int(value["av"]),
                policy_version=int(value["pv"]),
                routing_epoch=int(value["re"]),
                issued_at=float(value["iat"]),
                expires_at=float(value["exp"]),
            )
            now = time.time()
            if (
                claims.audience != self.audience
                or claims.expires_at <= now
                or claims.expires_at - claims.issued_at > 60.001
                or claims.issued_at > now + 5
                or not claims.ticket_id
                or not claims.principal
                or (
                    claims.principal.startswith("api:")
                    and (
                        claims.token_id is None
                        or claims.principal != "api:" + claims.token_id
                        or not claims.token_scopes
                    )
                )
                or (claims.token_id is not None and not claims.principal.startswith("api:"))
            ):
                return None
            # Validate enum and context construction before recording consumption.
            claims.context("ticket-validation")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        with self._lock:
            self._prune(now)
            if claims.ticket_id in self._consumed or len(self._consumed) >= self.max_consumed:
                return None
            self._consumed[claims.ticket_id] = claims.expires_at
        return claims

    def _prune(self, now: float) -> None:
        for ticket_id, expires_at in list(self._consumed.items()):
            if expires_at <= now:
                self._consumed.pop(ticket_id, None)


class RevocationState:
    """Local revocation high-water marks; ticket expiry is the hard fallback."""

    def __init__(self, *, retention_seconds: int = 120):
        self.retention_seconds = retention_seconds
        self._marks: dict[tuple[str, str], tuple[float, float]] = {}
        self._lock = threading.Lock()

    def apply(self, payload: str | dict) -> None:
        value = json.loads(payload) if isinstance(payload, str) else payload
        at = float(value.get("at", time.time()))
        expires = time.time() + self.retention_seconds
        updates = [
            (kind, str(value[key]))
            for kind, key in (
                ("principal", "principal"),
                ("user", "user_id"),
                ("tenant", "tenant_id"),
            )
            if value.get(key)
        ]
        with self._lock:
            self._prune(time.time())
            for key in updates:
                current = self._marks.get(key)
                if current is None or current[0] < at:
                    self._marks[key] = (at, expires)

    def ensure_active(self, claims: StreamClaims) -> None:
        now = time.time()
        if claims.expires_at <= now:
            raise AccessError(401, "流授权已过期,请重新连接")
        with self._lock:
            self._prune(now)
            marks = [
                self._marks.get(("principal", claims.principal)),
                self._marks.get(("user", claims.user_id)),
                self._marks.get(("tenant", claims.tenant_id)),
            ]
        if any(mark and claims.issued_at <= mark[0] for mark in marks):
            raise AccessError(401, "流授权已撤销,请重新连接")

    def _prune(self, now: float) -> None:
        for key, (_, expires_at) in list(self._marks.items()):
            if expires_at <= now:
                self._marks.pop(key, None)


class RevocationListener:
    """Dedicated PostgreSQL LISTEN connection for gateway revocation."""

    def __init__(self, dsn: str, state: RevocationState | None = None):
        self.dsn = dsn
        self.state = state or RevocationState()
        self.stop = threading.Event()
        self.connected = False
        self.last_notification_at: float | None = None
        self.thread = threading.Thread(target=self._loop, daemon=True, name="stream-revocations")

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def ensure_active(self, claims: StreamClaims) -> None:
        self.state.ensure_active(claims)

    def status(self) -> dict:
        return {
            "connected": self.connected,
            "last_notification_at": self.last_notification_at,
        }

    def _loop(self) -> None:
        import psycopg

        while not self.stop.is_set():
            try:
                with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
                    conn.execute(f"LISTEN {ACCESS_CHANNEL}")
                    self.connected = True
                    while not self.stop.is_set():
                        for notification in conn.notifies(timeout=1, stop_after=1):
                            self.state.apply(notification.payload)
                            self.last_notification_at = time.time()
            except Exception:
                logger.warning("stream revocation listener unavailable")
            finally:
                self.connected = False
            self.stop.wait(1)


def notify_access_change(conn, **values) -> None:
    payload = {key: value for key, value in values.items() if value}
    payload["at"] = time.time()
    conn.execute(
        f"SELECT pg_notify('{ACCESS_CHANNEL}',%s)",
        (json.dumps(payload, separators=(",", ":")),),
    )
