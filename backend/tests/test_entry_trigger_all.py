"""Selected entry points gate candidates together; exits still accept any point."""
from datetime import date

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext, StrategyEngine


@pytest.mark.parametrize("missing", [False, True])
def test_all_entry_points_gate_candidates_and_keep_independent_any_exit(tmp_path, missing):
    (tmp_path / "entry_all.py").write_text(
        'import polars as pl\n'
        'META = {"id": "all", "name": "ALL", "scoring": {}, "limit": 30}\n'
        'BASIC_FILTER = {"enabled": False}\n'
        'ENTRY_SIGNALS = ["one", "csg_two"]\n'
        'EXIT_SIGNALS = ["exit_one", "exit_two"]\n'
        'def filter(df, params): return pl.lit(True)\n',
    )
    engine = StrategyEngine([tmp_path])
    current = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH", "600003.SH", "600004.SH", "600005.SH"],
        "date": [date(2026, 8, 25)] * 5,
        "close": [10.] * 5, "signal_one": [True, True, False, True, True],
        "csg_two": [True, False, True, None, True],
        "signal_exit_one": [False, False, False, False, True],
        "signal_exit_two": [False] * 5,
    })
    if missing:
        current = current.drop("csg_two")
    context = StrategyDataContext("stock", "1d", date(2026, 8, 25), current=current)
    result = engine.run("all", context)
    assert [r["symbol"] for r in result.rows] == ([] if missing else ["600001.SH"])
    assert result.entry_signal_hits == ([] if missing else [
        {"symbol": "600001.SH", "signals": ["one", "csg_two"]}])
    assert result.exit_signal_hits == [{"symbol": "600005.SH", "signals": ["exit_one"]}]


def test_empty_entry_and_single_entry_keep_existing_behavior(tmp_path):
    (tmp_path / "entry_all.py").write_text(
        'import polars as pl\n'
        'META = {"id": "all", "name": "ALL", "scoring": {}}\n'
        'BASIC_FILTER = {"enabled": False}\n'
        'def filter(df, params): return pl.lit(True)\n',
    )
    engine = StrategyEngine([tmp_path])
    current = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"], "signal_one": [True, False]})
    ctx = StrategyDataContext("stock", "1d", date(2026, 8, 25), current=current)
    assert engine.run("all", ctx).total == 2
    result = engine.run("all", ctx, overrides={"entry_signals": ["one"]})
    assert [r["symbol"] for r in result.rows] == ["600001.SH"]


@pytest.mark.parametrize("missing", [False, True])
def test_daily_backtest_entry_mask_requires_all_and_exit_mask_stays_any(missing):
    from app.backtest.strategy import StrategyBacktestService

    service = object.__new__(StrategyBacktestService)
    panel = pl.DataFrame({"signal_one": [True, True, False, True],
                          "csg_two": [True, False, True, None]})
    if missing:
        panel = panel.drop("csg_two")
    names = ["one", "csg_two"]
    allowed = service._build_entry_mask_from_candidate(
        panel, pl.Series([True] * 4), None, names)
    assert allowed.to_list() == ([False] * 4 if missing else [True, False, False, False])
    assert service._build_signal_mask(panel, names, "_exit").to_list() == (
        [True, True, False, True] if missing else [True] * 4)


def test_entry_rule_change_fences_cached_identity_without_editing_saved_strategy(tmp_path, monkeypatch):
    from app.services.strategy_results import capture_identity
    from app.strategy import version

    code = ('import polars as pl\n'
            'META = {"id": "all", "name": "ALL", "scoring": {}}\n'
            'ENTRY_SIGNALS = ["one", "two"]\n'
            'def filter(df, params): return pl.lit(True)\n')
    path = tmp_path / "entry_all.py"
    path.write_text(code)
    engine = StrategyEngine([tmp_path])
    current = capture_identity(engine, None, "all", dependencies={}, lifecycle={})
    monkeypatch.setattr(version, "ENTRY_TRIGGER_POLICY_VERSION", "legacy_any", raising=False)
    legacy = capture_identity(engine, None, "all", dependencies={}, lifecycle={})
    assert current.fingerprint != legacy.fingerprint
    assert current.runtime_identity != legacy.runtime_identity
    assert path.read_text() == code


@pytest.mark.parametrize("overrides", [{}, {"entry_signals": []}, {"entry_signals": ["one"]}])
def test_minimal_strategy_identity_matches_explicit_empty_entry_configuration(overrides):
    from types import SimpleNamespace

    from app.strategy.version import runtime_identity

    minimal = SimpleNamespace(meta={"id": "minimal", "params": []}, logic_version="same")
    explicit = SimpleNamespace(**vars(minimal), entry_signals=[])
    assert runtime_identity(minimal, overrides) == runtime_identity(explicit, overrides)
