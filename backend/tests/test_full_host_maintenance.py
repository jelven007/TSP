"""Production backup and conservative retention helpers."""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest

from scripts.backup_full_host import create_backup, parse_include, sha256_file
from scripts.maintain_full_host import docker_prune_commands, release_candidates


def test_quiesced_backup_restores_both_databases_before_atomic_publish(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if "pg_dump" in command:
            database = command[-1]
            kwargs["stdout"].write(b"PGDMP-" + database.encode())
        if "pg_restore" in command:
            assert kwargs["stdin"].read(5) == b"PGDMP"
        if "psql" in command:
            return subprocess.CompletedProcess(command, 0, stdout="6|12\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    config = tmp_path / "edge.conf"
    config.write_text("upstream fixed;\n")
    target = tmp_path / "backup-final"
    manifest = create_backup(
        container="postgres",
        control_database="control_prod",
        data_database="data_prod",
        target=target,
        includes={"edge": config},
        runner=runner,
    )

    assert manifest["complete"] and manifest["quiesced"]
    assert manifest["restore_verification"]["control"]["schema_version"] == 6
    assert manifest["restore_verification"]["data"]["schema_version"] == 6
    assert (target / "files/edge").read_text() == "upstream fixed;\n"
    stored = json.loads((target / "manifest.json").read_text())
    assert stored == manifest
    for name in ("control", "data"):
        dump = target / f"{name}.dump"
        assert manifest["databases"][name]["sha256"] == sha256_file(dump)
    assert sum("pg_dump" in command for command in calls) == 2
    assert sum("pg_restore" in command for command in calls) == 2
    assert sum("dropdb" in command for command in calls) == 2
    assert not any(path.name.startswith(".backup-final.") for path in tmp_path.iterdir())


def test_backup_refuses_ambiguous_include_and_existing_target(tmp_path):
    with pytest.raises(ValueError, match="invalid include"):
        parse_include("../edge=/tmp")
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(FileExistsError):
        create_backup(
            container="postgres",
            control_database="control",
            data_database="data",
            target=target,
            includes={},
        )


def test_release_retention_preserves_current_recent_and_referenced(tmp_path):
    root = tmp_path / "public"
    releases = root / "releases"
    releases.mkdir(parents=True)
    now = time.time()
    ages = {
        "a" * 32: 60,
        "b" * 32: 50,
        "c" * 32: 40,
        "d" * 32: 2,
        "e" * 32: 1,
    }
    for release_id, days in ages.items():
        path = releases / release_id
        path.mkdir()
        os.utime(path, (now - days * 86400, now - days * 86400))
    (root / "current").symlink_to("releases/" + "a" * 32, target_is_directory=True)

    candidates = release_candidates(
        root,
        protected={"c" * 32},
        keep=2,
        min_age_days=30,
        now=now,
    )
    assert [path.name for path in candidates] == ["b" * 32]


def test_docker_prune_never_removes_tagged_rollback_images():
    commands = docker_prune_commands(until="168h", keep_storage="20GB")
    assert commands[0][:3] == ["docker", "builder", "prune"]
    assert commands[1][:3] == ["docker", "image", "prune"]
    assert all("-a" not in command and "--all" not in command for command in commands)
