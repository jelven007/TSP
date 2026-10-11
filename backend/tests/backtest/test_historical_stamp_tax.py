from datetime import date

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.matrix import build_market_matrix
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService


@pytest.mark.parametrize("day,rate", [
    ("2016-01-04", 0.001), ("2023-08-27", 0.001), ("2023-08-28", 0.0005),
    ("2026-10-09", 0.0005),
])
def test_historical_stock_tax_uses_exit_date(day, rate):
    cfg = MatcherConfig(stamp_tax_mode="historical", fees_pct=0, slippage_bps=0)
    assert cfg.sell_cost_pct(day) == rate
    assert cfg.buy_cost_pct() == 0


def test_historical_tax_exempts_etfs_and_requires_supported_date():
    cfg = MatcherConfig(stamp_tax_mode="historical", asset_type="etf", fees_pct=0, slippage_bps=0)
    assert cfg.sell_cost_pct("2026-10-09") == 0
    with pytest.raises(ValueError, match="2008"):
        MatcherConfig(stamp_tax_mode="historical").sell_cost_pct("2008-09-18")


@pytest.mark.parametrize("matrix", [False, True])
@pytest.mark.parametrize("exit_day,rate", [(date(2023, 8, 25), 0.001), (date(2023, 8, 28), 0.0005)])
def test_portfolio_tax_is_charged_on_actual_sale_in_both_engines(matrix, exit_day, rate):
    panel = pl.DataFrame({
        "symbol": ["600001.SH"] * 3,
        "date": [date(2023, 8, 23), date(2023, 8, 24), exit_day],
        **{key: [10.0] * 3 for key in ("open", "high", "low", "close")},
        "volume": [100000.0] * 3,
    })
    entries = pl.Series([True, False, False])
    exits = pl.Series([False] * 3)
    cfg = MatcherConfig(
        matching="open_t+1", max_positions=1, max_hold_days=1,
        fees_pct=0, slippage_bps=0, stamp_tax_mode="historical",
    )
    engine = BacktestEngine(None)
    result = engine.simulate_market_matrix(build_market_matrix(
        panel, entries, exits, entry_delay_bars=1, exit_delay_bars=1,
    ), cfg) if matrix else (
        engine.simulate_portfolio(panel, entries, exits, cfg)
    )
    trade = result.trades[0]
    assert trade.exit_date == exit_day.isoformat()
    assert trade.pnl_amount == pytest.approx(-trade.shares * 10 * rate)


def test_strategy_serializes_cost_policy_to_make_defaults_reproducible():
    cfg = StrategyBacktestConfig("s", None, date(2016, 1, 1), date(2026, 10, 9))
    serialized = StrategyBacktestService._config_to_dict(cfg)
    assert serialized["stamp_tax_mode"] == "historical"
    assert serialized["cost_model_version"] == "cn_stock_tax_v1"
    cfg.stamp_tax_pct = 0.0
    assert StrategyBacktestService._config_to_dict(cfg)["stamp_tax_mode"] == "fixed"


def test_research_gate_reports_changed_auxiliary_inputs():
    from app.backtest.governance import research_preflight

    config = StrategyBacktestConfig("s", None, date(2016, 1, 1), date(2026, 10, 9))
    trace = {"release_consistent": True, "research_inputs_match_release": False}
    issues = research_preflight(config, trace)
    assert len(issues) == 1
    assert "指纹" in issues[0]
    assert trace["research_governance"]["status_and_cost_gate_passed"] is False
