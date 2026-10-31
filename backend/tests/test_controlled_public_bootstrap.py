"""An operator-provided password remains valid for first public initialization."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from app.cell import admin, deployment
from app.cell.passwords import hash_password, verify_password


class Control:
    def __init__(self):
        self.user = None

    def transaction(self):
        return nullcontext()

    def execute(self, statement, parameters=None):
        if statement.startswith("SELECT id FROM control.users"):
            return SimpleNamespace(fetchone=lambda: None)
        if statement.lstrip().startswith("INSERT INTO control.users"):
            self.user = parameters
        return SimpleNamespace(fetchone=lambda: None)


def test_public_bootstrap_accepts_password_from_controlled_configuration(monkeypatch):
    password = "fixture-public-controlled-password"
    monkeypatch.setattr(admin, "ADMIN_PASSWORD", password)
    control = Control()
    assert admin.bootstrap_full_identity(control, password=password, public=True)
    assert verify_password(password, control.user[2], control.user[3])


@pytest.mark.parametrize("password", ["", "too-short"])
def test_public_bootstrap_rejects_missing_or_short_password(password):
    with pytest.raises(ValueError, match="12-128"):
        admin.bootstrap_full_identity(Control(), password=password, public=True)


def test_public_gate_accepts_strong_controlled_credentials(monkeypatch):
    password = "fixture-public-controlled-password"
    secret = "fixture-public-registration-secret"
    monkeypatch.setattr(deployment, "ADMIN_PASSWORD", password)
    monkeypatch.setattr(deployment, "REGISTRATION_SECRET", secret)
    salt, digest = hash_password(password)
    secret_salt, secret_digest = hash_password(secret)

    class Credentials:
        def execute(self, statement):
            if "JOIN control.user_roles" in statement:
                return SimpleNamespace(fetchall=lambda: [{"password_salt": salt, "password_hash": digest}])
            return SimpleNamespace(fetchone=lambda: {"secret_salt": secret_salt, "secret_hash": secret_digest})

    deployment.check_public_credentials(Credentials())
