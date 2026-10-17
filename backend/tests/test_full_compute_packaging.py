from __future__ import annotations

from pathlib import Path

from app.services.backtest import BacktestService

ROOT = Path(__file__).resolve().parents[2]


def test_compute_image_installs_frozen_signal_backtest_runtime() -> None:
    dockerfile = (
        ROOT / "deploy" / "full" / "Compute.Dockerfile"
    ).read_text(encoding="utf-8")

    assert "uv export --quiet --frozen --no-dev --extra backtest" in dockerfile
    assert "--no-emit-package tsp-rustdx-native --no-emit-project" in dockerfile
    assert "uv pip install --python /app/.venv/bin/python --require-hashes" in dockerfile
    assert 'UV_DEFAULT_INDEX="$PYPI_LOCK_MIRROR"' in dockerfile
    assert '.venv/bin/python -c "import vectorbt"' in dockerfile


def test_compute_acceptance_executes_signal_backtest() -> None:
    acceptance = (
        ROOT / "scripts" / "verify_full_compute.py"
    ).read_text(encoding="utf-8")
    worker = (
        ROOT / "backend" / "app" / "multitenancy" / "compute_worker.py"
    ).read_text(encoding="utf-8")

    assert '"kind": "signal_backtest"' in acceptance
    assert '"signal_backtest": "passed"' in acceptance
    assert "BacktestService(repo, persist_result=False)" in worker


def test_signal_backtest_persistence_remains_enabled_outside_compute() -> None:
    assert BacktestService(object()).persist_result is True
    assert BacktestService(object(), persist_result=False).persist_result is False
