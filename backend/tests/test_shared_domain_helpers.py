"""Shared readers and validation can run without importing HTTP route modules."""
import ast
import builtins
import importlib
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField
from app.strategy import custom_signals


def test_extension_values_are_available_without_api_imports(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "app.api" or name.startswith("app.api."):
            pytest.fail(f"shared reader imported HTTP layer: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert importlib.util.find_spec("app.services.ext_values") is not None
    reader = importlib.import_module("app.services.ext_values")
    config = ExtConfig(id="sample", label="sample", mode="timeseries",
                       fields=[ExtField("symbol", "string", "symbol"), ExtField("v", "float", "v")])
    ExtConfigStore(tmp_path).upsert(config)
    for day, value in [("2026-11-18", 1.0), ("2026-11-19", 2.0)]:
        partition = tmp_path / "ext_data" / "sample" / "timeseries" / f"date={day}"
        partition.mkdir(parents=True)
        pl.DataFrame({"symbol": ["000001.SZ"], "v": [value]}).write_parquet(partition / "part.parquet")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path, db=None))
    assert reader.load_ext_value_maps(repo, "sample.v") == {"sample__v": {"000001.SZ": 2.0}}


def test_custom_signal_reference_validation_uses_domain_registry(tmp_path):
    assert hasattr(custom_signals, "missing_custom_signals")
    with custom_signals.private_signal_registry([{"id": "existing"}]):
        assert custom_signals.missing_custom_signals(
            tmp_path, ["close", "csg_existing", None, "csg_missing"],
        ) == ["csg_missing"]


@pytest.mark.parametrize("relative,forbidden", [
    ("services/quote_service.py", "app.api.screener"),
    ("jobs/daily_pipeline.py", "app.api.data"),
    ("multitenancy/strategies.py", "app.api.strategy"),
])
def test_background_paths_do_not_depend_on_api_helpers(relative, forbidden):
    source = Path(__file__).parents[1] / "app" / relative
    imports = [node.module for node in ast.walk(ast.parse(source.read_text()))
               if isinstance(node, ast.ImportFrom)]
    assert forbidden not in imports
