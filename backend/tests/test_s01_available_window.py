"""Available-window source policy and frozen-window compatibility."""
from datetime import datetime

import polars as pl
from test_s01_optional_triggers import context, optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import bars, s01_engine

from app.strategy import custom_signals


def evaluate(frame, *, policy=True, params=None):
    engine = s01_engine()
    strategy = optional(engine)
    if policy:
        strategy.meta["entry_window_policy"] = "available_minutes_v1"
    ctx = context()
    ctx.history = frame
    with custom_signals.private_signal_registry([ENTRY, EXIT]):
        return engine.run("s01", ctx, params=params)


def sparse():
    return bars(price=10.25).filter(
        pl.col("datetime") != datetime(2026, 8, 25, 10))


def test_sparse_completed_minutes_can_enter_without_volume_gate_or_filling_gaps():
    result = evaluate(sparse().drop("volume"))
    assert result.total == 1
    assert result.rows[0]["amount"] == 224 * 1025000


def test_duplicate_minute_records_do_not_duplicate_amount_or_entry():
    frame = sparse()
    expected = evaluate(frame)
    result = evaluate(pl.concat([frame, frame.tail(1)]))
    assert expected.total == result.total == 1
    assert result.rows[0]["amount"] == expected.rows[0]["amount"]
    assert result.rows[0]["score"] == expected.rows[0]["score"]


def test_frozen_source_cannot_enable_the_new_window_via_runtime_parameter():
    assert evaluate(sparse(), policy=False).total == 0
    assert evaluate(sparse(), policy=False,
                    params={"_s01_entry_window_policy": "available_minutes_v1"}).total == 0
    assert evaluate(bars(price=10.25), policy=False).total == 1


def test_sparse_amount_threshold_and_empty_input_remain_effective():
    assert evaluate(sparse().with_columns(pl.lit(1.).alias("amount"))).total == 0
    assert evaluate(pl.DataFrame()).total == 0
