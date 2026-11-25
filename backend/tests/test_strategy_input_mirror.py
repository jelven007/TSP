"""The mirror consumes only a controlled, tree-verified input package."""
import importlib
import importlib.util
from pathlib import Path

import pytest
from test_strategy_input_authority import store
from test_strategy_input_manifest import build, minutes, request

PACK_SCRIPT = str(Path(__file__).resolve().parents[2] / "scripts/release/package_transfer.py")


def module():
    name = "app.services.strategy_input_mirror"
    assert importlib.util.find_spec(name), "controlled input mirror is missing"
    return importlib.import_module(name)


def test_package_roundtrip_keeps_exact_phase_and_input_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_PACK_SCRIPT", PACK_SCRIPT)
    origin, local = store(tmp_path / "origin"), store(tmp_path / "local")
    sealed = origin.seal(build(minutes()))
    from app.services.strategy_input_manifest import common_key
    key = common_key(request())
    bundle = module().package(origin, key, "stocks")
    value = module().install(local, bundle, key, "stocks")
    assert value["input_sha256"] == sealed["input_sha256"]
    assert local.read(request(), "stocks") == sealed


def test_corrupt_archive_and_foreign_phase_never_install(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_PACK_SCRIPT", PACK_SCRIPT)
    origin, local = store(tmp_path / "origin"), store(tmp_path / "local")
    origin.seal(build(minutes()))
    from app.services.strategy_input_manifest import common_key
    key = common_key(request())
    bundle = module().package(origin, key, "stocks")
    with pytest.raises(ValueError):
        module().install(local, {**bundle, "sha256": "f" * 64}, key, "stocks")
    assert local.read(request(), "stocks") is None
    with pytest.raises(ValueError, match="身份"):
        module().install(local, bundle, key, "breadth")
    assert local.read(request(), "breadth") is None


def test_package_returning_after_deadline_is_diagnostic_and_never_seals(tmp_path, monkeypatch):
    monkeypatch.setenv("TSP_PACK_SCRIPT", PACK_SCRIPT)
    from app.services.strategy_input_manifest import common_key
    origin, local = store(tmp_path / "origin"), store(tmp_path / "local")
    sealed = origin.seal(build(minutes()))
    key = common_key(request())
    bundle = module().package(origin, key, "stocks")
    value = module().install(local, bundle, key, "stocks", accept=lambda: False)
    assert value is None
    assert local.read(request(), "stocks") is None
    import json
    diagnostic = json.loads(local.path(key, "stocks").with_suffix(".diagnostic.json").read_text())
    assert diagnostic["diagnostic_only"]
    assert diagnostic["input_sha256"] == sealed["input_sha256"]
