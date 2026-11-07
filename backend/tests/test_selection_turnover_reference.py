"""Preselection turnover must come from the exact previous trading session."""
# ruff: noqa: F811 -- reuse the real minute-selection fixture.
from dataclasses import replace

import polars as pl
import pytest
from test_minute_signal_sections import scan  # noqa: F401

from app.strategy.signal_sections import select_rows


@pytest.mark.parametrize("turnover", [2.1, None])
def test_reference_turnover_never_uses_target_or_future_daily_rows(scan, turnover):
    engine, context, overrides = scan
    previous = context.daily_history.with_columns(
        pl.lit(turnover, dtype=pl.Float64).alias("turnover_rate"))
    future = previous.with_columns(pl.lit(context.as_of).alias("date"),
                                   pl.lit(99.).alias("turnover_rate"))
    current = context.current.with_columns(pl.lit(90.).alias("turnover_rate"))
    rows = select_rows(engine, engine.get("custom_test"),
                      replace(context, current=current,
                              daily_history=pl.concat([previous, future])),
                      overrides=overrides)
    assert len(rows) == 3
    assert all(row["reference_date"] == "2026-10-30" for row in rows)
    assert all(row["reference_turnover_rate"] == turnover for row in rows)
