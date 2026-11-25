"""A saved S01 generic entry cannot fail before its saved confirmation clock."""
from datetime import datetime

import polars as pl
import pytest
import test_s01_independent_entry as independent
from test_s01_independent_entry import DAY, FROZEN, OVERRIDES, SID

from app.market_time import CN_TZ
from app.strategy.daily_pool import DEFINITION_VERSION
from app.strategy.engine import StrategyDataContext
from app.strategy.signal_sections import pending_entry_time

engine = independent.engine


def context(clock, *, realtime=True):
    observed = datetime.fromisoformat(f"{DAY}T{clock}").replace(tzinfo=CN_TZ)
    prior = pl.DataFrame({"symbol": ["600001.SH"], "date": [datetime(2026, 11, 20).date()],
                          "raw_close": [10.], "close": [10.]})
    return StrategyDataContext(
        "stock", "1m", DAY, current=prior, pool_history=prior,
        observed_at=observed, completed_through=observed,
        realtime=realtime, history=None, input_errors=[f"{DAY} 无分钟K数据"],
        selection_snapshots={SID: FROZEN},
        daily_pool_snapshots={SID: {
            "strategy_id": SID, "as_of": str(DAY), "symbols": ["600001.SH"], "total": 1,
            "definition_version": DEFINITION_VERSION,
        }},
    )


@pytest.mark.parametrize("clock", ["05:31:07", "14:44:59", "14:45:00", "14:45:15", "14:45:59"])
def test_saved_s01_missing_minutes_wait_until_generic_confirmation(engine, clock):
    ctx = context(clock)
    result = engine.run(SID, ctx, overrides=OVERRIDES)
    assert result.section_statuses["buy"] == "waiting", result.section_errors
    assert pending_entry_time(engine, engine.get(SID), OVERRIDES, ctx) == datetime(
        2026, 11, 23, 14, 46)
    assert result.sections["buy"] == result.entry_signal_hits == []
    assert result.sections["select"] == FROZEN["rows"]
    assert "14:46:00" in result.section_errors["buy"]
    assert "无分钟K" not in result.section_errors["buy"]


@pytest.mark.parametrize("clock,realtime", [("14:46:00", True), ("15:00:00", False)])
def test_saved_s01_due_or_historical_missing_minutes_remain_failures(engine, clock, realtime):
    ctx = context(clock, realtime=realtime)
    assert pending_entry_time(engine, engine.get(SID), OVERRIDES, ctx) is None
    result = engine.run(SID, ctx, overrides=OVERRIDES)
    assert result.section_statuses["buy"] == "failed", result.section_errors
    assert "无分钟K" in result.section_errors["buy"]


def test_saved_s01_unselected_clock_does_not_delay_base_candidates(engine):
    override = {**OVERRIDES, "entry_signals": []}
    assert pending_entry_time(engine, engine.get(SID), override, context("05:31:07")) is None
