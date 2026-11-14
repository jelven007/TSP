"""Exercise equal-selected budgets through the real minute cash ledger."""
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from test_s01_optional_triggers import optional
from test_s01_strategy import BUILTINS, bars, daily

from app.api.strategy import _strategy_detail
from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestConfig,
    StrategyBacktestService,
)
from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine

MODE = "equal_selected_v1"
START, SECOND, END = date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27)


def service(tmp_path, count, *, partial=False, retained=False, equal=True):
    symbols = [f"60000{i + 1}.SH" for i in range(max(1, count))]
    panels, partitions = [], {d: [] for d in (START, SECOND, END)}
    for i, symbol in enumerate(symbols):
        previous = daily().with_columns(
            pl.lit(symbol).alias("symbol"), pl.lit(10. + i * .01).alias("close"),
            pl.lit(10. + i * .01).alias("raw_close"))
        panels.append(pl.concat([previous, *[
            previous.tail(1).with_columns(pl.lit(d).alias("date")) for d in partitions]]))
        for day in partitions:
            frame = bars(day, end=890, symbol=symbol).with_columns(pl.lit(1e6).alias("volume"))
            if retained and day == START and i >= 2:
                frame = frame.head(1)  # Not a complete first-day candidate.
            if partial and i == 0:
                frame = frame.with_columns(pl.lit(10.).alias("volume"))
            partitions[day].append(frame)
    panel = pl.concat(panels)
    partitions = {d: pl.concat(frames) for d, frames in partitions.items()}

    class Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        def get_instruments_asset(self, asset_type):
            return pl.DataFrame({"symbol": symbols, "name": ["正常股票"] * len(symbols),
                                 "listing_date": [date(2020, 1, 1)] * len(symbols)})

        def list_minute_dates(self, start, end, asset_type):
            return [d for d in partitions if start <= d <= end]

        def get_minute_by_dates(self, requested, days, asset_type):
            return partitions[days[0]]

    class Engine:
        repo = Repo()

        def data_generation(self, asset_type):
            return None

        def load_panel_for_backtest(self, *args, **kwargs):
            return panel

    engine = StrategyEngine([BUILTINS])
    definition = optional(engine)
    if equal:
        definition.meta["entry_allocation"] = MODE
    return StrategyBacktestService(Engine(), engine)


def run(subject, *, overrides=None, **params):
    return subject.run(StrategyBacktestConfig(
        "s01", None, START, END, params={"initial_cash": 1000000, "position_limit": 4,
                                        "entry_nav_fraction": .25, "prior_return_min": 0, **params},
        overrides={"entry_signals": [], "exit_signals": [], **(overrides or {})}),
        result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_selected_count_freezes_equal_fee_inclusive_budget(tmp_path, count):
    result = run(service(tmp_path, count))
    assert result.error is None
    positions = result.stats["execution"]["remaining_positions"]
    n = min(count, 4)
    assert len(positions) == n
    for position in positions:
        assert position["entry_budget"] == pytest.approx(1000000 / n)
        assert 1000000 / n - 1100 < position["entry_value"] <= 1000000 / n
    assert result.config["entry_allocation"] == MODE
    assert result.strategy_info["entry_allocation"] == MODE
    assert "entry_nav_fraction" not in result.config["params"]
    assert "entry_nav_fraction" not in result.strategy_info["params"]


@pytest.mark.parametrize("threshold, count", [(10.025, 2), (100, 0)])
def test_signal_filter_precedes_divisor_and_empty_selection_keeps_cash(tmp_path, threshold, count):
    definition = {"id": "selected", "name": "前日筛选", "kind": "entry", "enabled": True,
                  "conditions": [{"left": "close", "op": ">", "right": str(threshold)}]}
    with custom_signals.private_signal_registry([definition]):
        result = run(service(tmp_path, 5), overrides={"entry_signals": ["csg_selected"]})
    assert result.error is None
    positions = result.stats["execution"]["remaining_positions"]
    assert len(positions) == count
    if count:
        assert {p["symbol"] for p in positions} == {"600004.SH", "600005.SH"}
        assert all(p["entry_budget"] == 500000 for p in positions)
    else:
        assert result.fills == []
        assert result.stats["execution"]["cash"] == 1000000


def test_partial_fill_does_not_reassign_unused_budget(tmp_path):
    result = run(service(tmp_path, 2, partial=True))
    assert result.error is None
    positions = {p["symbol"]: p for p in result.stats["execution"]["remaining_positions"]}
    assert positions["600001.SH"]["shares"] == 4000
    assert positions["600001.SH"]["entry_value"] == pytest.approx(4000 * 10.11 * 1.00026)
    assert positions["600002.SH"]["entry_budget"] == 500000
    assert 498900 < positions["600002.SH"]["entry_value"] <= 500000
    assert result.stats["execution"]["cash"] > 459000


def test_retained_positions_use_slots_and_new_batch_divides_remaining_cash(tmp_path):
    result = run(service(tmp_path, 5, retained=True, partial=True))
    assert result.error is None
    positions = result.stats["execution"]["remaining_positions"]
    assert len(positions) == 4
    original = [p for p in positions if p["entry_date"] == START]
    added = [p for p in positions if p["entry_date"] == SECOND]
    assert len(original) == len(added) == 2
    assert all(p["entry_budget"] == 500000 for p in original)
    assert all(p["entry_budget"] == pytest.approx(result.equity_curve[0]["cash"] / 2) for p in added)


def test_extra_amount_cap_still_limits_each_equal_share(tmp_path):
    result = run(service(tmp_path, 2), entry_budget=100000)
    assert result.error is None
    assert len(result.stats["execution"]["remaining_positions"]) == 2
    assert all(p["entry_budget"] == 100000
               for p in result.stats["execution"]["remaining_positions"])


def test_equal_mode_cannot_exceed_four_positions(tmp_path):
    result = run(service(tmp_path, 5), position_limit=5)
    assert result.error and "4" in result.error
    assert result.fills == []


def test_legacy_source_retains_fixed_nav_fraction(tmp_path):
    result = run(service(tmp_path, 2, equal=False))
    assert result.error is None
    assert len(result.stats["execution"]["remaining_positions"]) == 2
    assert all(p["entry_value"] <= 250000 for p in result.stats["execution"]["remaining_positions"])
    assert result.config["params"]["entry_nav_fraction"] == .25
    assert "entry_allocation" not in result.config


def test_detail_exposes_mode_and_removes_inactive_fixed_fraction(tmp_path):
    subject = service(tmp_path, 2)
    detail = _strategy_detail(subject.strategy_engine.get("s01"),
                              {"params": {"entry_nav_fraction": .25}}, subject.strategy_engine)
    assert detail["entry_allocation"] == MODE
    assert "entry_nav_fraction" not in detail["params_defaults"]
    assert all(p["id"] != "entry_nav_fraction" for p in detail["params"])
    assert "entry_nav_fraction" not in detail["definition_defaults"]["params_defaults"]
