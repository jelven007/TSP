"""Current custom S01 uses available minutes; frozen sources keep their window."""
from datetime import datetime
from pathlib import Path

import polars as pl
import pytest
from test_s01_optional_triggers import DAILY_ENTRY, context, optional
from test_s01_parameter_signals import EXIT
from test_s01_security_validity import current
from test_s01_strategy import BUILTINS, bars

from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine

POLICY = "available_minutes_v1"
FIXTURE = Path(__file__).parent / "fixtures/s01_available_minutes.py"


def evaluate(frame, *, signal=None, completed=None, legacy=False):
    engine = StrategyEngine([BUILTINS])
    strategy = optional(engine)
    if not legacy:
        strategy.meta["entry_window_policy"] = POLICY
    ctx = context(end=890)
    ctx.history = frame
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    if completed:
        ctx.completed_through = completed
    with custom_signals.private_signal_registry([signal or current(), EXIT]):
        return engine.run("s01", ctx)


def sparse():
    return bars(price=10.25).filter(
        pl.col("datetime") != datetime(2026, 8, 25, 10))


def test_available_sparse_minutes_are_selected_without_filling_the_gap():
    result = evaluate(sparse())
    assert result.total == 1
    row = result.rows[0]
    assert row["count"] == 224
    assert row["amount"] == pytest.approx(224 * 1025000)
    assert row["last_datetime"] == datetime(2026, 8, 25, 14, 45)


def test_duplicate_records_are_normalized_without_double_amount_or_entries():
    frame = sparse()
    result = evaluate(pl.concat([frame, frame.tail(1)]))
    expected = evaluate(frame)
    assert result.total == expected.total == 1
    assert result.rows[0]["amount"] == expected.rows[0]["amount"]
    assert result.rows[0]["score"] == expected.rows[0]["score"]
    assert result.entry_data_quality == expected.entry_data_quality


def test_a_duplicate_cannot_manufacture_the_amount_threshold():
    one = bars(price=10.25).tail(1).with_columns(pl.lit(60000000.).alias("amount"))
    result = evaluate(pl.concat([one, one]))
    assert result.total == 0
    assert next(iter(result.entry_data_quality.values()))["market_valid_count"] == 1


def test_sparse_completed_prefix_ignores_future_and_uncompleted_bad_records():
    frame = bars(price=10.25, end=890).with_columns(
        pl.when(pl.col("datetime") >= datetime(2026, 8, 25, 14, 45))
        .then(-1.).otherwise(pl.col("amount")).alias("amount"))
    result = evaluate(frame, completed=datetime(2026, 8, 25, 14, 44))
    assert result.total == 1
    assert result.rows[0]["last_datetime"] == datetime(2026, 8, 25, 14, 44)


@pytest.mark.parametrize("values", [
    {"current_return_min": .026}, {"current_return_max": .024},
    {"current_range_max": .001}, {"current_amount_min": 1e9},
    {"market_min_valid": 2},
])
def test_original_entry_thresholds_remain_effective(values):
    assert evaluate(sparse(), signal=current(**values)).total == 0


def test_frozen_source_without_policy_keeps_its_original_complete_window():
    assert evaluate(sparse(), legacy=True).total == 0
    assert evaluate(pl.concat([bars(price=10.25)] * 2), legacy=True).total == 0
    assert evaluate(bars(price=10.25), legacy=True).total == 1
    assert evaluate(pl.DataFrame()).total == 0


@pytest.mark.parametrize("selection", [
    None, [], ["csg_daily_entry"],
    ["csgi_late_session_strength_s01", "csg_daily_entry"],
])
def test_actual_saved_source_all_entry_paths_allow_available_minutes(tmp_path, selection):
    from app.api.strategy import StrategyCodeSaveRequest, _prepare_strategy_code

    code = FIXTURE.read_text()
    _prepare_strategy_code(StrategyCodeSaveRequest(
        strategy_id="custom_mvxxcs01", mode="update", code=code))
    (tmp_path / "custom_mvxxcs01.py").write_text(code)
    engine = StrategyEngine([tmp_path])
    assert engine.has("custom_mvxxcs01"), engine.load_errors()
    ctx = context(end=890)
    ctx.history = sparse().drop("volume")
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    overrides = {"basic_filter": {"enabled": False}}
    if selection is not None:
        overrides["entry_signals"] = selection
    with custom_signals.private_signal_registry([current(), EXIT, DAILY_ENTRY]):
        result = engine.run("custom_mvxxcs01", ctx, overrides=overrides)
    assert result.total == 1
    assert result.rows[0]["amount"] == pytest.approx(224 * 1025000)
    assert result.rows[0]["score"] == pytest.approx(
        -.002 + .001 * __import__("math").log(224 * 1025000))


def test_policy_is_owned_by_source_and_cannot_be_enabled_by_runtime_params():
    engine = StrategyEngine([BUILTINS])
    strategy = optional(engine)
    ctx = context(end=890)
    ctx.history = sparse()
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    with custom_signals.private_signal_registry([current(), EXIT]):
        result = engine.run("s01", ctx, params={"_s01_entry_window_policy": POLICY})
    assert result.total == 0
    assert "entry_window_policy" not in strategy.meta
