"""Before a saved buy clock is due, unavailable intraday input is not failure."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from test_historical_buy_clock import scenario as _clock_scenario

from app.strategy import custom_signals

scenario = _clock_scenario


@pytest.mark.parametrize("offset", [None, -1, 15, 29])
def test_preconfirmation_missing_inputs_remain_waiting(scenario, offset):
    engine, sid, _, context, at = scenario
    observed = datetime.combine(context.as_of, datetime.min.time()).replace(hour=2, minute=34)
    if offset is not None:
        observed = at + timedelta(seconds=offset)
    ctx = replace(context, observed_at=observed, completed_through=observed,
                  history=None, realtime=True, input_errors=["当日无分钟K数据"])
    result = engine.run(sid, ctx)
    assert result.section_statuses["buy"] == "waiting", result.section_errors
    assert "当日无分钟K数据" not in result.section_errors["buy"]
    assert result.sections["select"] == context.selection_snapshots[sid]["rows"]
    assert result.sections["buy"] == []
    assert result.entry_signal_hits == []
    assert result.entry_data_quality
    for quality in result.entry_data_quality.values():
        progress = quality.get("progress") or {}
        assert progress, quality
        assert progress.get("state") != "failed", progress
        assert all(step.get("state") != "failed"
                   for step in (progress.get("steps") or {}).values()), progress


def test_due_missing_inputs_still_fail(scenario):
    engine, sid, _, context, at = scenario
    result = engine.run(sid, replace(
        context, observed_at=at + timedelta(seconds=30), history=None,
        realtime=True, input_errors=["当日无分钟K数据"]))
    assert result.section_statuses["buy"] == "failed"
    assert "当日无分钟K数据" in result.section_errors["buy"]
    assert result.sections["buy"] == []


def test_historical_missing_inputs_still_fail(scenario):
    engine, sid, _, context, _ = scenario
    result = engine.run(sid, replace(context, history=None, input_errors=["历史分钟分区损坏"]))
    assert result.section_statuses["buy"] == "failed"
    assert "历史分钟分区损坏" in result.section_errors["buy"]


def test_preconfirmation_does_not_hide_independent_exit_failure(scenario):
    engine, sid, _, context, at = scenario
    run = engine._run_strategy

    def evaluate(*args, **kwargs):
        args[1].exit_errors.append("持仓分钟分区损坏")
        return run(*args, **kwargs)

    engine._run_strategy = evaluate
    result = engine.run(sid, replace(
        context, observed_at=at - timedelta(minutes=1), history=None,
        realtime=True, input_errors=["当日无分钟K数据"]))
    assert result.section_statuses["buy"] == "waiting"
    assert result.section_statuses["sell"] == "failed"
    assert result.section_errors["sell"] == "持仓分钟分区损坏"


def test_s03_saved_thirty_second_clock_and_ten_thirty_deadline(scenario):
    engine, sid, sig, context, at = scenario
    if sid != "custom_mvxxcs03":
        pytest.skip("S03 saved configuration")
    sig = {**sig, "params": {**sig["params"], "end_time": "10:30:00"}}
    with custom_signals.private_signal_registry([sig]):
        for seconds, status in [(29, "waiting"), (30, "failed")]:
            result = engine.run(sid, replace(
                context, observed_at=at + timedelta(seconds=seconds),
                history=None, realtime=True, input_errors=["当日无分钟K数据"]))
            assert result.section_statuses["buy"] == status, result.section_errors
            assert result.entry_deadline_at == f"{context.as_of}T10:30:00"


def test_s03_grouped_entry_cannot_fail_before_confirmation(scenario):
    engine, sid, sig, context, at = scenario
    if sid != "custom_mvxxcs03":
        pytest.skip("S03 grouped entry")
    groups = {
        "a": {"enabled": True, "fund_min": 70, "rank_max": 20, "rank_min": 2,
              "tech_min": 75, "score_min": 55, "gap_max_pct": 10, "gap_min_pct": -1,
              "bias_max_pct": 100, "slope_min_pct": 1, "breadth_min_pct": None,
              "intraday_min_pct": .8, "momentum_max_pct": 40, "momentum_min_pct": 18,
              "position_min_pct": 90},
        "b": {"enabled": True, "fund_min": 40, "rank_max": 25, "rank_min": 6,
              "tech_min": 65, "score_min": 70, "gap_max_pct": 1, "gap_min_pct": -5,
              "bias_max_pct": 25, "slope_min_pct": 0, "breadth_min_pct": 25,
              "intraday_min_pct": None, "momentum_max_pct": 50, "momentum_min_pct": 5,
              "position_min_pct": None},
    }
    sig = {**sig, "params": {**sig["params"], "entry_groups": groups,
                            "change_min_pct": None, "change_max_pct": None,
                            "end_time": "10:30:00"}}
    with custom_signals.private_signal_registry([sig]):
        for seconds, status in [(-1, "waiting"), (15, "waiting"),
                                (29, "waiting"), (30, "failed")]:
            result = engine.run(sid, replace(
                context, observed_at=at + timedelta(seconds=seconds),
                history=None, realtime=True, input_errors=["当日无分钟K数据"]))
            assert result.section_statuses["buy"] == status, result.section_errors
            assert result.sections["buy"] == []
            if status == "waiting":
                assert all(step["state"] != "failed"
                           for quality in result.entry_data_quality.values()
                           for step in quality["progress"]["steps"].values())
