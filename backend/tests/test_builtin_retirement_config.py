"""部署退役仅影响内置发现, 保留自定义源与默认本地目录。"""
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.strategy.engine import StrategyEngine


def strategy_code(strategy_id):
    return f'''import polars as pl
META = {{"id": "{strategy_id}", "name": "{strategy_id}",
        "asset_types": ["stock"], "timeframes": ["1d"]}}
def filter(df, params):
    return pl.lit(True)
'''


def write_strategy(tmp_path, source, strategy_id, filename=None):
    directory = tmp_path / source
    directory.mkdir(exist_ok=True)
    (directory / (filename or f"{strategy_id}.py")).write_text(strategy_code(strategy_id))
    return directory


def test_deployment_retirement_defaults_to_empty(monkeypatch):
    monkeypatch.delenv("RETIRED_BUILTIN_STRATEGIES", raising=False)
    configured = Settings(_env_file=None)
    assert getattr(configured, "retired_builtin_strategies", None) == []


def test_deployment_retirement_reads_json_environment(monkeypatch):
    monkeypatch.setenv("RETIRED_BUILTIN_STRATEGIES", '["s01", "s02"]')
    configured = Settings(_env_file=None)
    assert getattr(configured, "retired_builtin_strategies", None) == ["s01", "s02"]


def test_retired_builtin_is_absent_after_initial_load_and_reload(tmp_path, monkeypatch):
    import app.strategy.engine as module

    builtin = write_strategy(tmp_path, "builtin", "s01", "renamed.py")
    write_strategy(tmp_path, "builtin", "s02")
    write_strategy(tmp_path, "builtin", "retained")
    monkeypatch.setattr(module, "settings", SimpleNamespace(retired_builtin_strategies=["s01", "s02"]))
    engine = StrategyEngine(strategy_dirs=[builtin])
    engine.reload()
    assert {meta["id"] for meta in engine.list_strategies()} == {"retained"}
    assert not engine.load_errors()
    for strategy_id in ("s01", "s02"):
        with pytest.raises(ValueError, match="unknown strategy"):
            engine.get(strategy_id)


@pytest.mark.parametrize("source", ["custom", "ai"])
def test_retirement_never_hides_a_private_source_with_the_same_id(tmp_path, monkeypatch, source):
    import app.strategy.engine as module

    builtin = write_strategy(tmp_path, "builtin", "s01")
    private = write_strategy(tmp_path, source, "s01")
    monkeypatch.setattr(module, "settings", SimpleNamespace(retired_builtin_strategies=["s01"]))
    engine = StrategyEngine(strategy_dirs=[builtin, private])
    engine.reload()
    assert not engine.load_errors()
    assert engine.get("s01").source == source


def test_empty_retirement_configuration_preserves_all_builtins(tmp_path, monkeypatch):
    import app.strategy.engine as module

    builtin = write_strategy(tmp_path, "builtin", "s01")
    write_strategy(tmp_path, "builtin", "s02")
    monkeypatch.setattr(module, "settings", SimpleNamespace(retired_builtin_strategies=[]))
    engine = StrategyEngine(strategy_dirs=[builtin])
    assert {meta["id"] for meta in engine.list_strategies()} == {"s01", "s02"}
