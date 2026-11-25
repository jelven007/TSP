"""A partial startup-minute publication must not become an immutable outcome."""
from copy import deepcopy
from datetime import timedelta

import polars as pl
import pytest
from test_late_session_optional_params import AT, DAY, signal
from test_late_session_strength import POOL, bars
from test_s02_selected_stock_scope import selection

from app.strategy.late_session_strength import evaluate


def run(frame, state, phase, *, references=None, selected=None):
    return evaluate(
        frame, signal(time="14:30:00", evaluation_delay_minutes=.5,
                      pool_up_min_pct=None, change_min_pct=.8, change_max_pct=4.,
                      ma5_1m_count=7, ma5_5m_count=4),
        day=DAY, pool_symbols=POOL,
        prev_close=dict.fromkeys(POOL, 10.) if references is None else references,
        completed_through=AT + timedelta(minutes=2), task_state=state,
        selection=selection(["a", "b"]) if selected is None else selected,
        independent_phase=phase,
    )


@pytest.mark.parametrize("phase", ["breadth", "stocks"])
@pytest.mark.parametrize("timezone", [None, "UTC"])
def test_partial_startup_waits_then_converges_and_freezes(phase, timezone):
    complete = bars()
    partial = complete.filter(~((pl.col("symbol") == "b") & (pl.col("datetime") == AT)))
    # A later price cannot replace the missing fixed startup row.
    future = complete.filter((pl.col("symbol") == "b") & (pl.col("datetime") == AT)).with_columns(
        pl.lit(AT + timedelta(minutes=1)).alias("datetime"), pl.lit(100.).alias("close"))
    partial = pl.concat([partial, future])
    if timezone:
        complete = complete.with_columns(
            pl.col("datetime").dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone(timezone))
        partial = partial.with_columns(
            pl.col("datetime").dt.replace_time_zone("Asia/Shanghai").dt.convert_time_zone(timezone))
    state = {}
    waiting = run(partial, state, phase)
    assert waiting.stage == "waiting_data" and waiting.diagnostics
    assert "terminal" not in state["independent_v1"][phase]
    if phase == "breadth":
        assert waiting.pool_up_pct == 50.
    else:
        assert waiting.progress["steps"]["change"]["passed"] == 1
    corrected = run(complete, state, phase)
    assert not corrected.diagnostics
    if phase == "breadth":
        assert corrected.pool_up_pct == 60.
    else:
        assert corrected.symbols == ["a", "b"]
    frozen = deepcopy(corrected)
    assert run(None, state, phase) == frozen


@pytest.mark.parametrize("phase", ["breadth", "stocks"])
def test_present_but_invalid_startup_price_is_isolated_without_global_wait(phase):
    complete = bars()
    bad_last = complete.filter((pl.col("symbol") == "b") & (pl.col("datetime") == AT)).with_columns(
        pl.lit(float("nan")).alias("close"))
    state = {}
    result = run(pl.concat([complete, bad_last]), state, phase)
    assert not result.diagnostics
    assert "terminal" in state["independent_v1"][phase]
    if phase == "breadth":
        assert result.pool_up_pct == 50.
    else:
        assert result.symbols == ["a"]


def test_invalid_reference_is_isolated_when_all_startup_rows_have_arrived():
    state = {}
    result = run(bars(), state, "breadth", references=dict.fromkeys(["a", "c", "d", "e"], 10.))
    assert result.pool_up_pct == 50. and not result.diagnostics
    assert result.missing_symbols == ["b"]
    assert "terminal" in state["independent_v1"]["breadth"]


def test_successful_empty_selection_needs_no_startup_rows():
    result = run(None, {}, "stocks", selected=selection([]))
    assert result.stage == "conditions_rejected" and not result.diagnostics
