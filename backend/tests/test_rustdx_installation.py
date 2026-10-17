"""Default dependency installation and offline native bridge validation."""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.plugins.rustdx import client

BACKEND = Path(__file__).resolve().parents[1]


def test_default_install_requires_native_bridge_without_extra() -> None:
    project = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((BACKEND / "uv.lock").read_text(encoding="utf-8"))
    backend = next(package for package in lock["package"] if package["name"] == "tsp-backend")

    assert "tsp-rustdx-native" in project["project"]["dependencies"]
    assert "psycopg[binary,pool]>=3.2,<4" in project["project"]["dependencies"]
    assert {"name": "tsp-rustdx-native"} in backend["dependencies"]
    assert {"name": "psycopg", "extra": ["binary", "pool"]} in backend["dependencies"]
    native = next(
        dependency
        for dependency in backend["metadata"]["requires-dist"]
        if dependency["name"] == "tsp-rustdx-native"
    )
    assert "marker" not in native
    assert native["directory"] == "native/rustdx_native"
    postgres = next(
        dependency
        for dependency in backend["metadata"]["requires-dist"]
        if dependency["name"] == "psycopg"
    )
    assert "marker" not in postgres
    assert postgres["extras"] == ["binary", "pool"]
    assert "rustdx" in project["project"]["optional-dependencies"]


def _bridge(**overrides) -> SimpleNamespace:
    def unexpected_connection(*_args, **_kwargs):
        pytest.fail("installation validation must not open a connection")

    return SimpleNamespace(
        **{
            "MAX_CONNECTIONS": 35,
            "__version__": "0.1.1",
            "RUSTDX_VERSION": "1.12.2",
            "RUSTDX_SOURCE_REV": "9dfc024c9662fa5091099c1f17610569ee2eca8b",
            "POOL_API_VERSION": 2,
            "RustdxClient": unexpected_connection,
            **overrides,
        }
    )


def test_installation_check_accepts_locked_bridge_without_network(monkeypatch, capsys) -> None:
    monkeypatch.setattr(client.importlib, "import_module", lambda _name: _bridge())

    assert client.main() == 0
    output = capsys.readouterr()
    assert "1.12.2" in output.out
    assert "9dfc024" in output.out
    assert output.err == ""


@pytest.mark.parametrize(
    "override",
    [
        {"RUSTDX_VERSION": "1.12.0"},
        {"RUSTDX_SOURCE_REV": "outdated"},
        {"POOL_API_VERSION": 1},
        {"MAX_CONNECTIONS": 36},
    ],
)
def test_installation_check_rejects_incompatible_bridge(monkeypatch, capsys, override) -> None:
    monkeypatch.setattr(client.importlib, "import_module", lambda _name: _bridge(**override))

    assert client.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "rustdx 原生桥接" in output.err
    assert "uv sync --frozen --reinstall-package tsp-rustdx-native" in output.err


def test_installation_check_rejects_missing_bridge(monkeypatch, capsys) -> None:
    def missing(_name):
        raise ModuleNotFoundError("No module named 'tsp_rustdx_native'")

    monkeypatch.setattr(client.importlib, "import_module", missing)

    assert client.main() == 1
    assert "缺少 rustdx 原生桥接" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="Bash launcher runs on macOS/Linux")
@pytest.mark.parametrize("extras", ["", "legacy-cpu"])
@pytest.mark.parametrize("bridge_status", [0, 1])
def test_launcher_syncs_existing_environment_and_stops_on_missing_bridge(
    tmp_path, extras, bridge_status
) -> None:
    """Run the real launcher with isolated commands; never touch live ports."""
    shutil.copyfile(BACKEND.parent / "dev.sh", tmp_path / "dev.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = tmp_path / "backend" / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    (tmp_path / "frontend" / "node_modules").mkdir(parents=True)
    commands = {
        bin_dir / "uv": 'printf \'uv %s\\n\' "$*" >> "$CALL_LOG"\n',
        bin_dir / "pnpm": 'printf \'pnpm %s\\n\' "$*" >> "$CALL_LOG"\n',
        bin_dir / "lsof": "exit 1\n",
        python: (
            'printf \'python %s\\n\' "$*" >> "$CALL_LOG"\n'
            f"exit {bridge_status}\n"
        ),
    }
    for path, body in commands.items():
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
    log = tmp_path / "calls.log"
    result = subprocess.run(
        ["bash", str(tmp_path / "dev.sh")],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "BACKEND_EXTRAS": extras,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    calls = log.read_text(encoding="utf-8")
    assert "uv sync --frozen --inexact" in calls
    assert ("--extra legacy-cpu" in calls) == bool(extras)
    assert "python -m app.plugins.rustdx.client" in calls
    if bridge_status:
        assert result.returncode != 0
        assert "uv run" not in calls
        assert "pnpm dev" not in calls
    else:
        assert result.returncode == 0, result.stderr
        assert "uv run --no-sync python -m uvicorn" in calls
        assert "pnpm dev" in calls
