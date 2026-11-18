"""Parameter-free S01 must use the common replay form and retain frozen inputs."""
import pytest
from test_s01_equal_selected import END, START, service

from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig


def replay(tmp_path, *, params=None, count=2, max_positions=2):
    subject = service(tmp_path, count)
    subject.strategy_engine.get("s01").meta["params"] = []
    return subject.run(StrategyBacktestConfig(
        "s01", None, START, END,
        params={"prior_return_min": 0, **(params or {})},
        overrides={"entry_signals": [], "exit_signals": []},
        initial_capital=1000000, max_positions=max_positions,
        commission_pct=.0002, minimum_commission=123,
        transfer_fee_pct=.0005, stamp_tax_pct=.001, slippage_bps=20),
        result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))


def test_parameter_free_s01_consumes_common_capital_positions_and_fee_controls(tmp_path):
    result = replay(tmp_path)
    assert result.error is None
    assert result.config["initial_capital"] == 1000000
    assert result.config["max_positions"] == 2
    assert result.config["commission_pct"] == .0002
    assert result.config["minimum_commission"] == 123
    assert result.config["transfer_fee_pct"] == .0005
    assert result.config["stamp_tax_pct"] == .001
    assert result.config["slippage_bps"] == 20
    positions = result.stats["execution"]["remaining_positions"]
    assert len(positions) == 2
    assert all(p["entry_budget"] == 500000 for p in positions)
    assert result.stats["execution"]["cash"] >= 0
    assert sum(f["notional"] + f["fee"] for f in result.fills) + result.stats["execution"]["cash"] == pytest.approx(1000000)


def test_explicit_frozen_execution_parameters_retain_their_historical_precedence(tmp_path):
    result = replay(tmp_path, params={
        "initial_cash": 1000000, "position_limit": 4, "commission": .0004,
        "minimum_commission": 5, "transfer_fee": 0, "stamp_tax": .0005, "slippage": .001})
    assert result.error is None
    assert result.config["max_positions"] == 4
    assert result.config["commission_pct"] == .0004
    assert result.config["minimum_commission"] == 5
    assert result.config["transfer_fee_pct"] == 0
    assert result.config["stamp_tax_pct"] == .0005
    assert result.config["slippage_bps"] == 10


def test_common_position_default_reserves_unfilled_standard_slots(tmp_path):
    result = replay(tmp_path, count=5, max_positions=10)
    assert result.error is None
    assert result.config["max_positions"] == 10
    positions = result.stats["execution"]["remaining_positions"]
    assert len(positions) == 5
    assert all(p["entry_budget"] == 100000 for p in positions)
