from dataclasses import dataclass
from enum import StrEnum

SYMBOL_PATTERN = r"^(?:(?:60[0135]|68[89])\d{3}\.SH|(?:000|001|002|003|300|301)\d{3}\.SZ)$"


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


READ_PERMISSIONS = frozenset({"watchlist:read", "strategies:read", "alerts:read", "paper:read"})
WRITE_PERMISSIONS = frozenset({"watchlist:write", "strategies:write", "alerts:write", "paper:write"})
ROLE_PERMISSIONS = {
    Role.OWNER: READ_PERMISSIONS | WRITE_PERMISSIONS | {"members:manage", "tokens:manage"},
    Role.ADMIN: READ_PERMISSIONS | WRITE_PERMISSIONS | {"members:manage", "tokens:manage"},
    Role.MEMBER: READ_PERMISSIONS | WRITE_PERMISSIONS | {"tokens:manage"},
    Role.VIEWER: READ_PERMISSIONS,
}
TOKEN_PERMISSIONS = READ_PERMISSIONS | WRITE_PERMISSIONS


class AccessError(Exception):
    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    user_id: str
    cell_id: str
    role: Role
    permissions: frozenset[str]
    policy_version: int
    routing_epoch: int
    request_id: str
    token_id: str | None = None
    token_scopes: frozenset[str] | None = None

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise AccessError(403, "permission_denied")
