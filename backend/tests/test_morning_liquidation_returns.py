"""Independent raw-close return exits and a real two-stage liquidation clock."""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import custom_signals
from app.strategy.morning_liquidation import evaluate, parameters

DAY = date(2026, 9, 7)
AT = datetime(2026, 9, 7, 10)


def signal(**patch):
    return {"id": "morning_liquidation", "name": "早盘清仓", "kind": "exit",
            "timeframe": "intraday", "template": "morning_liquidation",
            "conditions": [], "enabled": True,
            "params": {"time": "10:30:00", "fallback_time": "10:45:00",
                       "rise_pct": 6, "fall_pct": 2, **patch}}


def position(**patch):
    return {"symbol": "a", "entry_date": "2026-09-04", "qty": 100,
            "available_qty": 100, "account_id": "own", "cycle_id": "batch",
            "entry_price": 18, **patch}


def frame(price=10, *, at=AT, high=12, low=8):
    return pl.DataFrame({"symbol": ["a"], "datetime": [at], "close": [float(price)],
                         "high": [high], "low": [low]})


def run(data=None, *, sig=None, now=AT, positions=None, prev_close=None, quotes=None):
    return evaluate(frame() if data is None else data, sig or signal(), day=DAY, now=now,
                    positions=positions if positions is not None else [position()],
                    prev_close={"a": 10} if prev_close is None else prev_close, quotes=quotes)


def test_new_template_defaults_and_roundtrip(tmp_path):
    fresh = {**signal(), "params": {}}
    assert parameters(fresh) == {"time": "10:30:00", "fallback_time": "10:45:00",
                                  "rise_pct": 6, "fall_pct": 6}
    custom_signals.save_one(tmp_path, signal(rise_pct=8, fall_pct=3))
    assert custom_signals.load_all(tmp_path) == [signal(rise_pct=8, fall_pct=3)]


@pytest.mark.parametrize("patch", [
    {"fallback_time": "10:30:00"}, {"fallback_time": "10:29:59"},
    {"fallback_time": "11:30:01"}, {"fallback_time": "10:4:00"},
    {"rise_pct": 0}, {"rise_pct": -1}, {"rise_pct": 101},
    {"fall_pct": 0}, {"fall_pct": -2}, {"fall_pct": True},
    {"fall_pct": float("nan")}, {"amplitude_pct": 6},
])
def test_invalid_new_parameters_are_rejected(patch):
    with pytest.raises(ValueError):
        parameters(signal(**patch))


@pytest.mark.parametrize("price,reason,change", [
    (10.6, "rise", .06), (9.8, "fall", -.02),
])
def test_each_inclusive_return_threshold_uses_raw_previous_close(price, reason, change):
    hit, = run(frame(price)).hits
    assert hit["reason"] == reason
    assert hit["change_pct"] == pytest.approx(change)
    assert hit["source"] == "completed_minute"
    assert hit["trigger_time"] == AT.isoformat()
    assert hit["available_qty"] == 100 and hit["cycle_id"] == "batch"


@pytest.mark.parametrize("price", [10, 10.599, 9.801])
def test_range_and_purchase_pnl_do_not_replace_current_return(price):
    assert not run(frame(price)).hits


def test_raw_minute_price_takes_priority_over_adjusted_close():
    data = frame(5.3).with_columns(pl.lit(10.6).alias("raw_close"))
    assert run(data).hits[0]["reason"] == "rise"


def test_no_future_price_and_restart_retains_first_completed_trigger():
    future = frame(11, at=AT + timedelta(minutes=1))
    assert not run(future).hits
    data = pl.concat([frame(10.6), frame(10, at=AT + timedelta(minutes=1))])
    hit, = run(data, now=AT + timedelta(minutes=2)).hits
    assert hit["reason"] == "rise" and hit["trigger_time"] == AT.isoformat()


def test_live_quote_uses_fresh_price_and_authoritative_raw_previous_close():
    quote = pl.DataFrame({"symbol": ["a"], "price": [9.8], "prev_close": [99.],
                          "quote_ts": [AT.replace(tzinfo=CN_TZ).timestamp()]})
    hit, = run(quotes=quote, now=AT + timedelta(seconds=4)).hits
    assert hit["reason"] == "fall" and hit["source"] == "live_quote"
    assert hit["change_pct"] == pytest.approx(-.02)
    assert not run(quotes=quote, now=AT + timedelta(seconds=16)).hits
    assert not run(quotes=quote, now=AT - timedelta(seconds=3)).hits
    assert not run(quotes=quote, prev_close={}).hits


def test_stop_and_fallback_work_without_quotes_and_only_for_remaining_qty():
    empty = pl.DataFrame()
    assert not run(empty, prev_close={}, now=AT.replace(minute=29, second=59)).hits
    stop, = run(empty, prev_close={}, now=AT.replace(minute=30)).hits
    assert stop["reason"] == "stop_time" and stop["trigger_time"].endswith("10:30:00")
    fallback, = run(empty, prev_close={}, now=AT.replace(minute=45),
                    positions=[position(qty=40, available_qty=40)]).hits
    assert fallback["reason"] == "fallback"
    assert fallback["trigger_time"].endswith("10:45:00") and fallback["available_qty"] == 40
    assert not run(empty, now=AT.replace(minute=45),
                   positions=[position(qty=0, available_qty=0)]).hits
    assert not run(empty, now=AT.replace(minute=45),
                   positions=[position(entry_date=str(DAY), available_qty=0)]).hits


def test_fallback_is_distinct_even_when_an_earlier_return_trigger_is_replayed():
    hit, = run(frame(10.6), now=AT.replace(minute=45)).hits
    assert hit["reason"] == "fallback" and hit["source"] == "clock"
    assert hit["trigger_time"].endswith("10:45:00")


def test_stop_retriggers_remaining_holdings_after_an_earlier_return_hit():
    hit, = run(frame(10.6), now=AT.replace(minute=30),
               positions=[position(qty=40, available_qty=40)]).hits
    assert hit["reason"] == "stop_time" and hit["source"] == "clock"
    assert hit["trigger_time"].endswith("10:30:00") and hit["available_qty"] == 40


def test_legacy_amplitude_definition_keeps_its_frozen_semantics():
    legacy = {**signal(), "params": {"time": "10:30:00", "amplitude_pct": 6}}
    assert run(frame(10, high=10.6, low=10), sig=legacy).hits[0]["reason"] == "amplitude"
    hit, = run(pl.DataFrame(), sig=legacy, now=AT.replace(minute=45)).hits
    assert hit["reason"] == "deadline" and hit["trigger_time"].endswith("10:30:00")


def test_monitor_dedup_keeps_separate_holding_exit_stages():
    from app.strategy.engine import StrategyEngine
    from app.strategy.monitor import MonitorRuleEngine

    monitor = MonitorRuleEngine(StrategyEngine([]))
    key = ("rule", "custom", "stock")
    assert monitor._new_strategy_signals(key, "sell_signal", DAY, []) == set()
    stop = {"symbol": "a", "signals": ["csgi_morning_liquidation"], "holding_exit": True,
            "account_id": "own", "cycle_id": "batch", "entry_date": "2026-09-04",
            "reason": "stop_time", "trigger_time": "2026-09-07T10:30:00"}
    assert monitor._new_strategy_signals(key, "sell_signal", DAY, [stop]) == {"a"}
    assert monitor._new_strategy_signals(key, "sell_signal", DAY, [stop]) == set()
    fallback = {**stop, "reason": "fallback", "trigger_time": "2026-09-07T10:45:00"}
    assert monitor._new_strategy_signals(key, "sell_signal", DAY, [fallback]) == {"a"}
    assert monitor._new_strategy_signals(key, "sell_signal", DAY, [fallback]) == set()


def test_signal_replay_preserves_early_return_and_later_fallback():
    from app.services.morning_liquidation_replay import replay

    class Repo:
        def get_daily_batch(self, *args, **kwargs):
            return pl.DataFrame({"symbol": ["a"], "date": [date(2026, 9, 4)],
                                 "close": [5.], "raw_close": [10.]})

        def get_minute_batch(self, *args, **kwargs):
            return frame(10.6)

    result = replay(Repo(), signal(), start=DAY, end=DAY, symbols=["a"], positions=[position()])
    assert [(hit["reason"], hit["time"]) for hit in result["triggers"]] == [
        ("rise", "10:00:00"), ("stop_time", "10:30:00"), ("fallback", "10:45:00"),
    ]


def test_signal_replay_does_not_substitute_an_older_previous_close():
    from app.services.morning_liquidation_replay import replay

    class Repo:
        def get_daily_batch(self, *args, **kwargs):
            return pl.DataFrame({"symbol": ["a"], "date": [date(2026, 9, 3)],
                                 "close": [10.], "raw_close": [10.]})

        def get_minute_batch(self, *args, **kwargs):
            return frame(10.6)

    result = replay(Repo(), signal(), start=DAY, end=DAY, symbols=["a"], positions=[position()])
    assert all(hit["reason"] in ("stop_time", "fallback") for hit in result["triggers"])


def test_live_engine_requires_exact_prior_session_for_new_return_definition(tmp_path):
    from test_morning_liquidation import sample_engine

    from app.strategy.engine import StrategyDataContext

    engine = sample_engine(tmp_path)
    custom_signals.save_one(tmp_path, signal())
    prior = pl.DataFrame({"symbol": ["a"], "date": [date(2026, 9, 3)],
                          "close": [5.], "raw_close": [10.]})
    context = StrategyDataContext("stock", "1m", DAY, history=frame(10.6),
                                  positions=[position()], observed_at=AT, pool_history=prior)
    overrides = {"exit_signals": ["csgi_morning_liquidation"]}
    with pytest.raises(ValueError, match="2026-09-04"):
        engine.run("sample", context, overrides=overrides)
    context.pool_history = prior.with_columns(pl.lit(date(2026, 9, 4)).alias("date"))
    hit, = engine.run("sample", context, overrides=overrides).exit_signal_hits
    assert hit["reason"] == "rise" and hit["change_pct"] == pytest.approx(.06)
