import json
import subprocess
from datetime import UTC, datetime

import pytest

from scripts.operations.backup_full_host import BUSINESS_CHECKS, create_backup
from scripts.operations.scheduled_full_backup import backup_age, scheduled_backup


def configuration(tmp_path):
    files = {}
    for name in ("compose", "env", "platform_data", "public_market", "platform_config",
                 "runtime_env", "platform_env"):
        target = tmp_path / name
        target.write_text("controlled input")
        files[name] = str(target)
    return {
        "compose_files": [files["compose"]], "env_file": files["env"],
        "includes": {key: value for key, value in files.items() if key not in {"compose", "env"}},
        "output_root": str(tmp_path / "backups"), "postgres_container": "pg",
        "control_database": "control", "data_database": "business",
    }


def test_scheduled_backup_refuses_running_writers_and_restarts_after_failure(tmp_path):
    config = configuration(tmp_path)
    calls = []
    statuses = ["running", "exited"]

    def runner(command, **kwargs):
        calls.append(command)
        if "ps" in command:
            state = statuses.pop(0)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([
                {"Service": "api", "State": state},
            ]))
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(RuntimeError, match="writers are running"):
        scheduled_backup(config, runner=runner, backup=lambda **_: pytest.fail("must not dump"))
    assert not any("stop" in command for command in calls)
    statuses[:] = ["running", "exited"]

    def broken_backup(**kwargs):
        raise RuntimeError("dump failed")

    with pytest.raises(RuntimeError, match="dump failed"):
        scheduled_backup(config, stop_writers=True, runner=runner, backup=broken_backup)
    assert any(command[-4:] == ["stop", "--timeout", "60", "api"] for command in calls)
    assert calls[-1][-3:] == ["up", "-d", "api"]


def test_restore_evidence_mismatch_prevents_backup_publication(tmp_path):
    def runner(command, **kwargs):
        if "pg_dump" in command:
            kwargs["stdout"].write(b"PGDMP-test")
        if "psql" in command:
            query = command[-1]
            if "json_build_object" in query:
                schema = "control" if "control.users" in query else "business"
                scratch = any(value.startswith("tsp_verify_") for value in command)
                values = dict.fromkeys(BUSINESS_CHECKS[schema], 1 if scratch else 0)
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps(values))
            return subprocess.CompletedProcess(command, 0, stdout="6|12")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    target = tmp_path / "backup"
    with pytest.raises(RuntimeError, match="differs"):
        create_backup(container="pg", control_database="control", data_database="business",
                      target=target, includes={}, runner=runner)
    assert not target.exists()
    assert not list(tmp_path.glob(".backup.*"))


def test_backup_age_distinguishes_missing_stale_and_unverified(tmp_path):
    now = datetime(2026, 10, 25, 12, tzinfo=UTC)
    assert backup_age(tmp_path, now=now)["age_hours"] is None
    directory = tmp_path / "one"
    directory.mkdir()
    manifest = {
        "complete": True, "created_at": "2026-10-24T11:00:00+00:00",
        "restore_verification": {name: {"restored": True, "source_matched": True}
                                 for name in ("control", "data")},
        "databases": {name: {"bytes": 5} for name in ("control", "data")},
    }
    for name in ("control", "data"):
        (directory / f"{name}.dump").write_bytes(b"PGDMP")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    assert backup_age(tmp_path, now=now)["ready"] is False
    assert backup_age(tmp_path, now=now, max_hours=30)["ready"] is True
    (directory / "data.dump").write_bytes(b"bad")
    assert backup_age(tmp_path, now=now, max_hours=30)["invalid_backups"] == ["one"]
