"""Read-only credential and schema gates for full-product deployment."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.bootstrap_defaults import ADMIN_PASSWORD, REGISTRATION_SECRET
from app.cell.passwords import valid_record, verify_password


def check_schema_history(conn, schema: str, expected: int) -> None:
    if schema not in {"control", "business"}:
        raise ValueError("unsupported schema")
    kind = "control" if schema == "control" else "data"
    rows = conn.execute(
        f"SELECT version,checksum FROM {schema}.schema_version ORDER BY version"
    ).fetchall()
    if [row["version"] for row in rows] != list(range(1, expected + 1)):
        raise RuntimeError("unsupported Cell database schema history")
    root = Path(__file__).with_name("sql")
    for row in rows:
        digest = hashlib.sha256((root / f"{kind}_v{row['version']}.sql").read_bytes()).hexdigest()
        if digest != row["checksum"]:
            raise RuntimeError("Cell database schema checksum mismatch")


def check_public_credentials(control) -> None:
    """Never repair accounts or expose password records in diagnostics."""
    admins = control.execute(
        """SELECT u.password_salt,u.password_hash FROM control.users u
           JOIN control.user_roles r ON r.user_id=u.id
           WHERE r.role_id='admin' AND u.active AND u.account_state='active'"""
    ).fetchall()
    if not admins:
        raise RuntimeError("public deployment requires an active administrator")
    for row in admins:
        if not valid_record(row["password_salt"], row["password_hash"]) or verify_password(
            ADMIN_PASSWORD, row["password_salt"], row["password_hash"],
        ):
            raise RuntimeError("public deployment administrator credential requires remediation")
    record = control.execute(
        "SELECT secret_salt,secret_hash FROM control.registration_config WHERE singleton=true"
    ).fetchone()
    if not record or not valid_record(record["secret_salt"], record["secret_hash"]) or verify_password(
        REGISTRATION_SECRET, record["secret_salt"], record["secret_hash"],
    ):
        raise RuntimeError("public deployment registration credential requires remediation")
