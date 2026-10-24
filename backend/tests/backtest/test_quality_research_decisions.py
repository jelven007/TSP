"""Research gates must not turn a selected historical result into approval."""

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def research(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts/research"))
    import research_quality_strategies

    return research_quality_strategies


def result(win=.4, expectancy=.01, pf=1.2, trades=500, returns=.1, dd=-.2):
    return {
        "metrics": {"win_rate": win, "expectancy": expectancy, "profit_factor": pf, "trades": trades},
        "stats": {"total_return": returns, "max_drawdown": dd},
    }


def test_higher_win_rate_with_lower_expectancy_is_rejected(research):
    decision = research.candidate_decision(result(), result(win=.7, expectancy=-.001))
    assert decision["decision"] == "rejected_topic"
    assert "expectancy_below_registered_gate" in decision["reasons"]


def test_few_trades_missing_stats_or_worse_drawdown_cannot_pass(research):
    for candidate, reason in (
        (result(win=.45, trades=249), "coverage_below_half"),
        (result(win=.45, dd=-.3), "max_drawdown_worse"),
        (result(win=.45, returns=None), "total_return_unavailable"),
    ):
        assert reason in research.candidate_decision(result(), candidate)["reasons"]


def test_all_historical_gates_pass_only_allows_exploratory_followup(research):
    decision = research.candidate_decision(result(), result(win=.44, expectancy=.02, pf=1.4))
    assert decision["decision"] == "exploratory_followup_only"
    assert decision["production_promotion"] is False


def test_trade_set_diagnostic_retains_avoided_loss_and_new_loser(research):
    def trade(symbol, pnl):
        return {"symbol": symbol, "entry_date": "2024-01-05", "pnl_pct": pnl, "pnl_amount": 100 * pnl}

    differences = research._differences(
        {"trades": [trade("A", .1), trade("B", -.1), trade("C", .02)]},
        {"trades": [trade("C", .03), trade("D", -.2)]},
    )
    assert differences["missed_winners"] == 1
    assert differences["avoided_losers"] == 1
    assert differences["same_stock_entry_day"] == 1
    assert differences["added_metrics"]["net_pnl"] == -20


def test_risk_candidates_are_separate_trials_with_unchanged_entry(research):
    args = SimpleNamespace(start="2021-10-18", end="2026-10-16", slippage_bps=5.)
    sid = "n_day_low_reversal"
    original = research._config(args, sid, "original", None)
    candidate = research._config(args, sid, "risk_budget_0p5pct", None)
    assert candidate.params == original.params
    assert candidate.overrides == {"entry_risk": {"risk_per_trade_pct": .005}}
    assert research.trial_variant(candidate) == "risk_budget_0p5pct"
    assert research.trial_variant(original) == "original"
    assert sum(len(research.variants_for(sid)) for _, ids in research.BUILTIN_FAMILIES.values() for sid in ids) == 73


def test_risk_improvement_does_not_require_a_win_rate_claim(research):
    decision = research.candidate_decision(result(), result(dd=-.15), objective="risk_return")
    assert decision["decision"] == "exploratory_followup_only"
    assert decision["production_promotion"] is False
    unchanged = research.candidate_decision(result(), result(), objective="risk_return")
    assert "drawdown_not_improved" in unchanged["reasons"]
