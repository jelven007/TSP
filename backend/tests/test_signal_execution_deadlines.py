"""Template clocks bound the real order window, including exact-second cutoffs."""
from datetime import datetime, time, timedelta

import polars as pl
import pytest
from test_morning_liquidation_returns import AT, frame, run, signal
from test_paper_s02_execution import DAY, advance, bars, entry
from test_paper_s02_execution import setup as paper_setup

from app.backtest.entry_execution import execute_entry_window
from app.strategy import custom_signals, paper
from app.strategy.morning_liquidation import parameters

setup = paper_setup


@pytest.mark.parametrize("at", ["00:00:00", "09:29:59", "11:30:01", "13:30:15", "23:59:59"])
def test_morning_clock_accepts_any_time_of_day(at):
    sig = {**signal(), "params": {
        "start_time": at, "time": None, "fallback_time": None,
        "rise_pct": 6, "fall_pct": None,
    }}
    assert parameters(sig)["start_time"] == at


def test_afternoon_return_signal_is_not_clipped_at_1130():
    at = AT.replace(hour=14, minute=3)
    sig = signal(time="14:20:00", fallback_time="14:30:00", start_time="13:00:00")
    hit, = run(frame(10.6, at=at), sig=sig, now=at).hits
    assert hit["reason"] == "rise" and hit["trigger_time"] == at.isoformat()


def test_fallback_is_a_cutoff_and_never_a_new_liquidation_signal():
    sig = {**signal(), "params": {
        "start_time": None, "time": None, "fallback_time": "10:45:00",
        "rise_pct": None, "fall_pct": None,
    }}
    assert not run(pl.DataFrame(), sig=sig, prev_close={}, now=AT.replace(minute=45)).hits


def test_fallback_keeps_the_original_trigger_and_marks_exact_cutoff_elapsed():
    hit, = run(frame(10.6), now=AT.replace(minute=45)).hits
    assert hit["reason"] == "rise" and hit["trigger_time"] == AT.isoformat()
    assert hit["deadline_elapsed"] is True


@pytest.mark.parametrize("end,minutes", [("14:40:00", 10), ("14:33:30", 3), ("14:31:00", 1)])
def test_buy_capacity_uses_end_time_instead_of_five_minutes(end, minutes):
    start = datetime.combine(DAY, time(14, 31))
    result = execute_entry_window(
        bars(start, n=12), symbol="600001.SH", start_at=start,
        end_at=datetime.combine(DAY, time.fromisoformat(end)),
        prior_close=10., budget=1000000., size_order=lambda _: 2000,
        buy_value=lambda value: value + max(5., value * .0002),
    )
    assert len(result.fills) == minutes
    assert result.filled_shares == minutes * 100
    assert result.cancelled_shares == 2000 - minutes * 100
    assert sum(fill.fee for fill in result.fills) == pytest.approx(result.fee)


def test_paper_buy_freezes_template_end_and_can_continue_past_five_minutes(setup):
    storage, _clock, _rule, repo, event = setup
    event["entry_policy"]["entry_definition"] = {
        "template": "late_session_strength", "params": {
            "time": "14:30:00", "end_time": "14:40:00", "evaluation_delay_minutes": .5,
            "pool_up_min_pct": None, "ma5_1m_count": None, "ma5_5m_count": None,
        },
    }
    order = entry(setup)
    assert order["strategy_execution"]["end_at"].endswith("14:40:00")
    start = datetime.combine(DAY, time(14, 31))
    repo.frame = bars(start, n=11)
    advance(setup, start + timedelta(minutes=7))
    assert len(paper.load_fills(storage)) == 8
    advance(setup, start + timedelta(minutes=9))
    assert len(paper.load_fills(storage)) == 10
    advance(setup, start + timedelta(minutes=10))
    assert len(paper.load_fills(storage)) == 10


def test_paper_sell_stops_at_fallback_second_before_processing_more_bars(setup):
    storage, _clock, _rule, repo, _event = setup
    entry(setup)
    repo.frame = bars(datetime.combine(DAY, time(14, 31)), volume=10.)
    advance(setup, datetime.combine(DAY, time(14, 35)))
    tomorrow = DAY + timedelta(days=1)
    repo.frame = bars(datetime.combine(tomorrow, time(10, 30)), n=16, volume=1.)
    advance(setup, datetime.combine(tomorrow, time(10, 30)))
    before = len(paper.load_fills(storage))
    advance(setup, datetime.combine(tomorrow, time(10, 45)))
    assert len(paper.load_fills(storage)) == before
    sell, = [order for order in paper.load_orders(storage) if order["side"] == "sell"]
    assert sell["status"] == "expired"


def test_frozen_template_roundtrip_does_not_add_sell_duration(tmp_path):
    saved = signal(time="13:15:00", fallback_time="14:00:15")
    custom_signals.save_one(tmp_path, saved)
    assert custom_signals.load_all(tmp_path) == [saved]
