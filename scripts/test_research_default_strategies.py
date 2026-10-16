from pathlib import Path

import pytest

from research_default_strategies import (
    RESEARCH_FAMILIES,
    candidate_trials,
    finite_json,
    monthly_block_comparison,
    prepare,
    rejection_reasons,
    trade_metrics,
)

from app.strategy.engine import StrategyEngine
from app.strategy.overlay import normalize_conditions


def test_metrics_use_net_trade_returns_and_gross_profit_over_gross_loss():
    trades = [
        {"pnl_pct": 0.10, "pnl_amount": 100.0},
        {"pnl_pct": 0.05, "pnl_amount": 50.0},
        {"pnl_pct": -0.025, "pnl_amount": -25.0},
        {"pnl_pct": 0.0, "pnl_amount": 0.0},
    ]
    metrics = trade_metrics(trades)
    assert metrics["trades"] == 4
    assert metrics["win_rate"] == 0.5
    assert metrics["expectancy"] == pytest.approx(0.03125)
    assert metrics["profit_factor"] == 6
    assert metrics["payoff_ratio"] == pytest.approx(3)
    assert metrics["win_rate_wilson95"][0] < 0.5 < metrics["win_rate_wilson95"][1]


def test_no_trades_and_no_losses_do_not_invent_finite_profit_factor():
    assert trade_metrics([])["win_rate"] is None
    assert trade_metrics([{"pnl_pct": 0.1, "pnl_amount": 10}])["profit_factor"] is None
    assert finite_json({"a": [float("nan"), float("inf")]}) == {"a": [None, None]}


def test_registered_hypotheses_cover_exactly_default_strategies():
    source = Path(__file__).resolve().parents[1] / "backend/app/strategy/builtin"
    ids = {item["id"] for item in StrategyEngine([source]).list_strategies()}
    declared = [sid for family in RESEARCH_FAMILIES.values() for sid in family]
    assert len(declared) == len(set(declared)) == 22
    assert set(declared) == ids
    trials = candidate_trials()
    assert len(trials) == 44
    assert all(sum(t["strategy_id"] == sid for t in trials) == 2 for sid in ids)
    for trial in trials:
        conditions = normalize_conditions(trial["overrides"]["overlay_filter"])
        assert len(conditions) == 1


@pytest.mark.parametrize("target_relative", [".", "nested"])
def test_snapshot_cannot_write_to_source_tree(tmp_path, target_relative):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="隔离"):
        prepare(source, source / target_relative, "rustdx", 160)


def test_snapshot_refuses_nonempty_unmarked_target(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("user data")
    with pytest.raises(ValueError, match="非空"):
        prepare(source, target, "rustdx", 160)
    assert marker.read_text() == "user data"


def test_validation_rejects_higher_win_rate_with_lower_expectancy_or_few_trades():
    original = {
        "metrics": {"trades": 300, "win_rate": 0.4, "expectancy": 0.01, "profit_factor": 1.2},
        "stats": {"max_drawdown": -0.15},
    }
    candidate = {
        "metrics": {"trades": 50, "win_rate": 0.8, "expectancy": 0.005, "profit_factor": 1.1},
        "stats": {"max_drawdown": -0.20},
    }
    assert set(rejection_reasons(candidate, original, "validation")) == {
        "expectancy_not_improved", "profit_factor_not_improved",
        "fewer_than_100_trades", "fewer_than_half_baseline_trades",
        "drawdown_worse_or_unavailable",
    }
    assert "win_rate_not_improved" in rejection_reasons(original, original, "train")


def test_month_bootstrap_pairs_calendar_blocks_and_is_reproducible():
    trades = [
        {"entry_date": "2022-01-04", "pnl_pct": 0.1},
        {"entry_date": "2022-02-04", "pnl_pct": -0.1},
        {"entry_date": "2022-02-08", "pnl_pct": -0.2},
    ]
    same = monthly_block_comparison(trades, trades, draws=100)
    assert same["win_rate_delta95"] == [0, 0]
    assert same["expectancy_delta95"] == [0, 0]
    improved = [{**t, "pnl_pct": t["pnl_pct"] + 0.5} for t in trades]
    first = monthly_block_comparison(trades, improved, draws=100)
    assert first == monthly_block_comparison(trades, improved, draws=100)
    assert first["expectancy_delta95"] == pytest.approx([0.5, 0.5])
    assert monthly_block_comparison([], [])["win_rate_delta95"] is None
