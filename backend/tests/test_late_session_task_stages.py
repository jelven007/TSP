"""Current nullable definitions follow the durable two-stage day task."""
from datetime import timedelta

import polars as pl
from test_late_session_optional_params import AT, DAY, bars, signal

from app.market_time import CN_TZ
from app.strategy.late_session_strength import evaluate


def run(sig, seconds, state, frame=None):
    return evaluate(
        bars() if frame is None else frame, sig, day=DAY, pool_symbols=["a"],
        prev_close={"a": 10.}, completed_through=AT + timedelta(seconds=seconds),
        task_state=state,
    )


def test_breadth_waits_fifteen_seconds_and_stock_inputs_wait_for_confirmation():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5,
                 pool_up_min_pct=60., amount_min_100m=1.)
    state = {}
    assert run(sig, 14, state).pool_up_pct is None
    early = run(sig, 15, state, bars().drop("amount"))
    assert early.pool_up_pct == 100.
    assert early.stage == "waiting_confirmation"
    assert early.progress["steps"]["change"]["state"] == "waiting"
    assert early.progress["steps"]["change"]["scheduled_at"] == (AT + timedelta(seconds=30)).isoformat()
    assert not early.diagnostics
    final = run(sig, 30, state)
    assert final.symbols == ["a"]


def test_first_successful_breadth_is_frozen_and_valid_rejection_is_terminal():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    state = {}
    assert run(sig, 15, state).pool_up_pct == 100.
    down = bars().with_columns(pl.lit(9.).alias("close"))
    assert run(sig, 30, state, down).pool_up_pct == 100.
    rejected = {}
    assert run(sig, 15, rejected, down).stage == "breadth_rejected"
    assert run(sig, 30, rejected).stage == "breadth_rejected"
    assert not run(sig, 30, rejected).symbols


def test_breadth_failure_retries_before_confirmation_then_expires():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    missing = bars().head(0)
    recovered = {}
    assert run(sig, 15, recovered, missing).diagnostics
    assert run(sig, 29, recovered).pool_up_pct == 100.
    expired = {}
    assert run(sig, 15, expired, missing).diagnostics
    result = run(sig, 30, expired)
    assert result.stage == "expired" and result.diagnostics
    assert not result.symbols


def test_stock_failure_retries_until_end_without_recalculating_breadth():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5,
                 end_time="14:31:00", pool_up_min_pct=60., amount_min_100m=1.)
    state = {}
    run(sig, 15, state)
    failed = run(sig, 30, state, bars().drop("amount"))
    assert failed.diagnostics and failed.stage == "waiting_data"
    recovered = run(sig, 59, state)
    assert recovered.symbols == ["a"]
    assert recovered.progress["confirmed_at"] == (AT + timedelta(seconds=59)).isoformat()
    expired = {}
    run(sig, 15, expired)
    run(sig, 30, expired, bars().drop("amount"))
    assert run(sig, 61, expired).stage == "expired"
    assert not run(sig, 61, expired).symbols


def test_valid_stock_zero_ends_the_day_and_later_data_cannot_reverse_it():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, amount_min_100m=2.)
    state = {}
    assert run(sig, 30, state).stage == "conditions_rejected"
    enough = bars().with_columns(pl.lit(2e8).alias("amount"))
    result = run(sig, 40, state, enough)
    assert result.stage == "conditions_rejected"
    assert result.symbols == []


def test_definition_or_pool_change_cannot_reuse_an_old_stage_receipt():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    state = {}
    run(sig, 15, state)
    sig["params"]["pool_up_min_pct"] = 100.
    down = bars().with_columns(pl.lit(9.).alias("close"))
    assert run(sig, 20, state, down).stage == "breadth_rejected"


def test_monitor_state_roundtrip_keeps_signal_receipts():
    from app.strategy.monitor import MonitorRuleEngine

    engine = MonitorRuleEngine()
    state = {}
    run(signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.), 15, state)
    engine._late_session_tasks = {"sample:csgi_late": state}
    restored = MonitorRuleEngine()
    restored.restore_state(engine.export_state())
    assert restored._late_session_tasks == engine._late_session_tasks


def test_engine_retries_saved_stage_and_reports_actual_confirmation_time(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, amount_min_100m=1.)
    sig.update(id="late", name="尾盘走强", kind="entry", timeframe="intraday", conditions=[])
    custom_signals.save_one(tmp_path, sig)
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    state = {}
    context = StrategyDataContext("stock", "1m", DAY, observed_at=AT + timedelta(seconds=30),
                                  signal_task_states=state)
    failed = engine._inject_intraday_signal_columns(
        bars().drop("amount"), context=context, pool_symbols=["a"])
    assert not failed["csgi_late"].any() and context.intraday_errors
    context.observed_at = AT + timedelta(seconds=40)
    recovered = engine._inject_intraday_signal_columns(bars(), context=context, pool_symbols=["a"])
    assert not context.intraday_errors
    assert recovered.filter(pl.col("csgi_late"))["entry_evaluated_at"].item() == context.observed_at
    assert state


def test_schedule_wakes_at_breadth_then_confirmation_and_retries_same_inputs():
    from app.strategy.late_session_strength import next_task_time

    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    assert next_task_time(sig, DAY, AT, {}) == AT + timedelta(seconds=15)
    state = {}
    run(sig, 15, state)
    assert next_task_time(sig, DAY, AT + timedelta(seconds=15), state) == AT + timedelta(seconds=30)
    failed = {}
    run(sig, 15, failed, bars().head(0))
    assert next_task_time(sig, DAY, AT + timedelta(seconds=16), failed) == AT + timedelta(seconds=16)
    run(sig, 30, state)
    assert next_task_time(sig, DAY, AT + timedelta(seconds=31), state) is None


def test_failures_alert_immediately_but_identical_retries_do_not_spam():
    from app.multitenancy.monitor_compute import failure_events

    failed = run(signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.),
                 15, {}, bars().head(0))
    daily = {"as_of": str(DAY), "entry_data_quality": {"csgi_late": {
        "stage": failed.stage, "progress": failed.progress,
    }}}
    previous = {}
    rules = [{"id": "rule", "strategy_id": "sample", "type": "strategy"}]
    events, state = failure_events({"sample": daily}, rules, previous, AT + timedelta(seconds=15))
    assert len(events) == 1 and events[0]["severity"] == "warning"
    assert events[0]["type"] == "signal_calculation_failed"
    assert "breadth" in events[0]["signals"]
    assert failure_events({"sample": daily}, rules, state, AT + timedelta(seconds=16))[0] == []


def test_disabling_a_signal_cannot_replay_its_saved_success():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5)
    state = {}
    assert run(sig, 30, state).symbols == ["a"]
    sig["enabled"] = False
    assert run(sig, 40, state).symbols == []


def test_failure_diagnostics_do_not_change_start_minute_or_use_later_amounts():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, amount_min_100m=2.)
    future = bars().tail(1).with_columns(
        (pl.col("datetime") + pl.duration(minutes=1)).alias("datetime"), pl.lit(3e8).alias("amount"))
    result = run(sig, 90, {}, pl.concat([bars(), future]))
    assert result.symbols == [] and result.stage == "conditions_rejected"


def test_completed_task_keeps_its_receipt_after_signal_validity_interval():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, end_time="14:31:00")
    state = {}
    first = run(sig, 30, state)
    assert first.symbols == ["a"]
    assert run(sig, 61, state) == first


def test_live_task_missing_the_entire_breadth_window_cannot_confirm_late():
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    assert run(sig, 35, {}).stage == "expired"


def test_context_failure_keeps_successful_breadth_without_a_loaded_pool():
    from app.multitenancy.monitor_compute import failed_task_results

    sig = signal(time="14:30:00", evaluation_delay_minutes=.5,
                 pool_up_min_pct=60., amount_min_100m=1.)
    sig["id"] = "late"
    state = {}
    run(sig, 15, state)
    identity = state["identity"]
    payload = {"state": {"engine": {"late_session_tasks": {"sample:probe:csgi_late": state}}},
               "late_task_definitions": {"sample": [sig]}, "late_task_scopes": {"sample": "sample:probe"}}
    failed = failed_task_results(payload, "分钟上下文暂不可用",
                                 (AT + timedelta(seconds=40)).replace(tzinfo=CN_TZ))
    assert state["identity"] == identity and state["breadth"]["pool_up_pct"] == 100.
    progress = failed["sample"]["entry_data_quality"]["csgi_late"]["progress"]
    assert progress["phase"] == "stocks" and "分钟上下文暂不可用" in progress["failure_reason"]
    assert run(sig, 45, state).symbols == ["a"]
