"""S01 must consume a delayed generic entry at its confirmation clock."""
from datetime import date, datetime

import pytest
from test_s01_optional_triggers import context, optional
from test_s01_strategy import BUILTINS, _service

from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine

ENTRY = {
    "id": "delayed_tail", "name": "尾盘确认", "kind": "entry", "enabled": True,
    "template": "late_session_strength", "timeframe": "intraday", "conditions": [],
    "params": {
        "time": "14:45:00", "evaluation_delay_minutes": 1, "end_time": "14:55:00",
        "pool_up_min_pct": 65, "change_min_pct": .5, "change_max_pct": 3,
        "amount_min_100m": 1, "amplitude_max_pct": 5.5,
        "turnover_min_pct": None, "volume_ratio_min": None,
        "ma5_1m_count": None, "ma5_5m_count": None,
    },
}


def configure(engine):
    strategy = optional(engine)
    strategy.meta.update(result_sections="signals", entry_window_policy="available_minutes_v1")
    strategy.entry_signals = ["csgi_delayed_tail"]
    strategy.exit_signals = []


def test_replay_consumes_confirmed_generic_tail_signal(tmp_path):
    service = _service(tmp_path)
    configure(service.strategy_engine)
    with custom_signals.private_signal_registry([ENTRY]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            initial_capital=1000000, params={"initial_cash": 1000000, "market_min_valid": 1},
        ), result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    buys = [fill for fill in result.fills if fill["side"] == "buy"]
    assert buys
    assert min(fill["bar"] for fill in buys) == 887
    assert result.stats["execution"]["remaining_positions"][0]["shares"] > 0


@pytest.mark.parametrize("observed, expected", [
    ("14:45:59", 0), ("14:46:00", 1),
])
def test_generic_confirmation_is_separate_from_its_completed_bar(observed, expected):
    engine = StrategyEngine([BUILTINS])
    configure(engine)
    ctx = context(price=10.1)
    ctx.observed_at = datetime.fromisoformat("2026-08-25T" + observed)
    ctx.selection_snapshots = {"s01": {
        "as_of": "2026-08-25", "source_date": "2026-08-24",
        "rows": [{"symbol": "600001.SH", "reference_close": 10.}],
    }}
    with custom_signals.private_signal_registry([ENTRY]):
        result = engine.run("s01", ctx)
    assert result.total == expected
    if expected:
        assert result.entry_data_quality["csgi_delayed_tail"]["stage"] == "confirmed"
        assert result.rows[0]["last_datetime"] == datetime(2026, 8, 25, 14, 45)
        assert result.rows[0]["entry_evaluated_at"] == datetime(2026, 8, 25, 14, 46)
