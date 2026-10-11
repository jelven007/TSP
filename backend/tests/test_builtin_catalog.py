"""Catalog cleanup must not leave dangling templates or invalidate older reports."""
from pathlib import Path

import pytest

from app.api.monitor_rules import _DEMO_STRATEGY_RULES
from app.strategy.engine import StrategyEngine
from scripts.run_governed_builtin_backtest import _validate_report_checkpoint

BUILTIN_DIR = Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"
RETIRED = {"trend_breakout", "limit_up_momentum", "broken_board_recovery"}


def test_retired_builtins_are_removed_after_reload_and_templates_still_resolve():
    engine = StrategyEngine(strategy_dirs=[BUILTIN_DIR])
    engine.reload()
    public = {meta["id"] for meta in engine.list_strategies()}

    assert not engine.load_errors()
    assert not public & RETIRED
    assert {"breakout_new_high_60d", "consecutive_limit_ups"} <= public
    assert "factor_rank_research" not in public
    assert engine.get("factor_rank_research").meta["research_only"]
    for rule in _DEMO_STRATEGY_RULES:
        assert rule["strategy_id"] in public
    for sid in RETIRED:
        with pytest.raises(ValueError, match="unknown strategy"):
            engine.get(sid)


@pytest.mark.parametrize("count", [2, 22, 25])
def test_report_checkpoint_uses_frozen_count_not_current_catalog(count):
    _validate_report_checkpoint({
        "run_spec": {"strategy_count": count},
        "strategies": [{"strategy_id": f"strategy_{i}", "error": None} for i in range(count)],
    })


def test_legacy_checkpoint_without_count_remains_readable():
    _validate_report_checkpoint({"strategies": [{"strategy_id": "legacy", "error": None}]})


@pytest.mark.parametrize("payload", [
    {"strategies": []},
    {"run_spec": {"strategy_count": 2}, "strategies": [{"strategy_id": "a"}]},
    {"strategies": [{"strategy_id": "a"}, {"strategy_id": "a"}]},
    {"strategies": [{"strategy_id": "a", "error": "failed"}]},
    {"strategies": [{"error": None}]},
])
def test_report_checkpoint_rejects_incomplete_or_failed_results(payload):
    with pytest.raises(RuntimeError, match="complete successful"):
        _validate_report_checkpoint(payload)
