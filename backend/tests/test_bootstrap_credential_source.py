"""Distributed source must obtain initialization credentials from controlled config."""
import json
import os
import subprocess
import sys
from pathlib import Path


def _defaults(tmp_path, content="", **variables):
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AUTH_", "TSP_CELL_"))
    }
    env.update(TICKFLOW_ENV_FILE=str(path), **variables)
    result = subprocess.run(
        [sys.executable, "-c",
         "import json; from app import bootstrap_defaults as b; "
         "print(json.dumps([b.ADMIN_USERNAME, b.ADMIN_PASSWORD, b.REGISTRATION_SECRET]))"],
        cwd=Path(__file__).resolve().parents[1],
        env=env, text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def test_source_has_no_embedded_initialization_credentials(tmp_path):
    username, password, registration = _defaults(tmp_path)
    assert username == "admin"
    assert password == ""
    assert registration == ""


def test_explicit_initialization_environment_is_preserved(tmp_path):
    assert _defaults(
        tmp_path, AUTH_USERNAME="fixture-admin", AUTH_PASSWORD="fixture-admin-secret",
        AUTH_REGISTRATION_SECRET="fixture-registration-secret",
    ) == ["fixture-admin", "fixture-admin-secret", "fixture-registration-secret"]


def test_controlled_dotenv_preserves_literal_password_characters(tmp_path):
    assert _defaults(
        tmp_path,
        "AUTH_USERNAME=fixture-admin\n"
        "AUTH_PASSWORD='fixture-${literal}-password'\n"
        "AUTH_REGISTRATION_SECRET='fixture-registration-口令'\n",
    ) == ["fixture-admin", "fixture-${literal}-password", "fixture-registration-口令"]


def test_local_administrator_keeps_fixed_username_contract(tmp_path):
    username, password, _ = _defaults(
        tmp_path, LOCAL_ADMIN_MODE="true", SERVICE_ROLE="standalone",
        AUTH_USERNAME="fixture-custom-name", AUTH_PASSWORD="fixture-local-password",
    )
    assert username == "admin"
    assert password == "fixture-local-password"
