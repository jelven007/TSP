"""Money-weighted PF differs from per-trade payoff, especially with flat trades."""
import json
from datetime import date

import pytest

from app.backtest.engine import BacktestEngine, SimulationOptions, TradeRecord


def _trades():
    # +10% on 1,000, -5% on 10,000, flat on 1,000:
    # win rate 1/3, payoff 2, amount PF .2, expectation 1/60.
    return [
        TradeRecord(
            symbol=str(i), entry_date=date(2024, 1, 2), exit_date=date(2024, 1, 3),
            entry_price=10, exit_price=10 * (1 + ret), pnl_pct=ret,
            duration=1, exit_reason="signal", entry_value=basis, pnl_amount=ret * basis,
        )
        for i, (ret, basis) in enumerate([(0.1, 1000), (-0.05, 10000), (0.0, 1000)])
    ]


@pytest.mark.parametrize("mode", ["legacy", "independent", "portfolio"])
def test_explicit_metrics_disambiguate_money_pf_and_return_payoff(mode):
    trades = _trades()
    if mode == "legacy":
        stats = BacktestEngine._calc_stats(
            trades, 100000, date(2024, 1, 2), date(2024, 1, 3), include_monte_carlo=False,
        )
    elif mode == "independent":
        stats = BacktestEngine._calc_independent_candidate_result(
            trades, 3, {}, options=SimulationOptions(include_monte_carlo=False),
        ).stats
    else:
        stats = BacktestEngine._calc_portfolio_stats(
            [{"value": 100000}, {"value": 99600}], trades, 100000, include_monte_carlo=False,
        )
    assert stats["profit_factor_amount"] == 0.2
    assert stats["payoff_ratio"] == 2.0
    assert stats["net_expectancy"] == pytest.approx(1 / 60, abs=1e-6)
    assert stats["win_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert (stats["n_wins"], stats["n_losses"], stats["n_flat"]) == (1, 1, 1)
    assert stats["profit_factor"] == 4.0  # untouched historical API (flat counted in losses)
    json.dumps(stats, allow_nan=False)


def test_legacy_trade_without_notional_does_not_invent_amount_pf():
    trades = _trades()
    trades[0].entry_value = 0
    stats = BacktestEngine._calc_stats(
        trades, 100000, date(2024, 1, 2), date(2024, 1, 3), include_monte_carlo=False,
    )
    assert stats["profit_factor_amount"] is None
    assert stats["payoff_ratio"] == 2.0


@pytest.mark.parametrize("trades", [[], _trades()[:1], _trades()[2:]])
def test_empty_winning_or_flat_samples_are_json_safe(trades):
    stats = BacktestEngine._calc_portfolio_stats(
        [{"value": 100000}], trades, 100000, include_monte_carlo=False,
    )
    assert stats["profit_factor_amount"] is None
    assert stats["payoff_ratio"] is None
    assert stats["n_losses"] == 0
    json.dumps(stats, allow_nan=False)
