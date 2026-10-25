"""Public deployments reject defaults; local upgrades preserve identities."""
import hashlib
import secrets
from contextlib import nullcontext
from pathlib import Path

import pytest

from app.bootstrap_defaults import ADMIN_PASSWORD, REGISTRATION_SECRET
from app.cell.admin import bootstrap_full_identity
from app.cell.deployment import check_public_credentials, check_schema_history
from app.cell.passwords import hash_password


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _Control:
    def __init__(self, admin=ADMIN_PASSWORD, secret=REGISTRATION_SECRET, *, existing=True):
        salt, digest = hash_password(admin)
        self.admins = [{"password_salt": salt, "password_hash": digest}]
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), 200_000).hex()
        self.secret = {"secret_salt": salt, "secret_hash": digest}
        self.existing = existing
        self.writes = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        if "INSERT" in query or "UPDATE control" in query:
            self.writes.append((query, params))
        if "SELECT id FROM control.users" in query:
            return _Rows([{"id": "existing"}] if self.existing else [])
        if "SELECT 1 FROM control.user_roles" in query:
            return _Rows([{"ok": 1}])
        if "JOIN control.user_roles" in query:
            return _Rows(self.admins)
        if "registration_config" in query:
            return _Rows([self.secret] if self.secret else [])
        return _Rows([])


def test_public_bootstrap_requires_explicit_password_without_writing():
    control = _Control(existing=False)
    with pytest.raises(ValueError, match="explicit administrator password"):
        bootstrap_full_identity(control, public=True)
    assert control.writes == []
    assert bootstrap_full_identity(control, public=True, password="explicit-long-password") is True
    assert len(control.writes) == 2


def test_existing_password_is_not_reset_by_deployment_configuration():
    control = _Control(admin="old-private-password")
    assert bootstrap_full_identity(control, public=True, password="new-private-password") is False
    assert control.writes == []
    assert bootstrap_full_identity(_Control(existing=False)) is True


@pytest.mark.parametrize("admin,secret,message", [
    (ADMIN_PASSWORD, "private-register-secret", "administrator"),
    ("private-admin-password", REGISTRATION_SECRET, "registration"),
])
def test_public_deployment_rejects_known_default_credentials(admin, secret, message):
    control = _Control(admin, secret)
    with pytest.raises(RuntimeError, match=message):
        check_public_credentials(control)
    assert control.writes == []


def test_public_deployment_accepts_rotated_credentials_and_rejects_unknown():
    control = _Control("private-admin-password", "private-register-secret")
    check_public_credentials(control)
    control.secret = None
    with pytest.raises(RuntimeError, match="registration"):
        check_public_credentials(control)
    control.admins = []
    with pytest.raises(RuntimeError, match="active administrator"):
        check_public_credentials(control)


def test_schema_history_checks_all_migrations_and_rejects_gaps():
    root = Path(__file__).resolve().parents[1] / "app" / "cell" / "sql"
    rows = [{"version": n, "checksum": hashlib.sha256(
        (root / f"control_v{n}.sql").read_bytes()).hexdigest()} for n in range(1, 7)]

    class Connection:
        def execute(self, _query):
            return _Rows(rows)

    check_schema_history(Connection(), "control", 6)
    original = rows[0]["checksum"]
    rows[0]["checksum"] = "changed"
    with pytest.raises(RuntimeError, match="checksum"):
        check_schema_history(Connection(), "control", 6)
    rows[0]["checksum"] = original
    del rows[2]
    with pytest.raises(RuntimeError, match="history"):
        check_schema_history(Connection(), "control", 6)
