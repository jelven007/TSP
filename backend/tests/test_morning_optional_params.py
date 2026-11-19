"""Optional morning conditions keep their own data and clock dependencies."""
from datetime import timedelta

import polars as pl
import pytest
from test_morning_liquidation_returns import AT, DAY, frame, position, run, signal

from app.services.morning_liquidation_replay import replay
from app.strategy import custom_signals
from app.strategy.morning_liquidation import parameters

EMPTY = dict.fromkeys((
    "start_time", "rise_pct", "fall_pct", "decline_3m_pct",
    "volume_ratio_min", "time", "fallback_time",
))


def optional(**patch):
    return {**signal(), "params": {**EMPTY, **patch}}


def window(prices=None, volumes=None, *, first=AT):
    size = len(prices if prices is not None else volumes)
    data = {"symbol": ["a"] * size, "datetime": [first + timedelta(minutes=i) for i in range(size)]}
    if prices is not None:
        data["raw_close"] = [float(p) for p in prices]
    if volumes is not None:
        data["volume"] = [float(v) for v in volumes]
    return pl.DataFrame(data)


def test_all_empty_roundtrip_disables_signal_and_dependencies(tmp_path):
    sig = optional()
    assert parameters(sig) == EMPTY
    custom_signals.save_one(tmp_path, sig)
    assert custom_signals.load_all(tmp_path) == [sig]
    assert not run(pl.DataFrame(), sig=sig, prev_close={}, now=AT.replace(hour=15)).hits


@pytest.mark.parametrize("key", ["rise_pct", "fall_pct", "decline_3m_pct", "volume_ratio_min"])
@pytest.mark.parametrize("value", [0, .09, 100.1, True, float("nan"), "1", ""])
def test_only_nonempty_numbers_must_be_finite_and_in_range(key, value):
    with pytest.raises(ValueError):
        parameters(optional(**{key: value}))


@pytest.mark.parametrize("key", ["rise_pct", "fall_pct", "decline_3m_pct", "volume_ratio_min"])
@pytest.mark.parametrize("value", [.1, 100, None])
def test_inclusive_number_boundaries(key, value):
    assert parameters(optional(**{key: value}))[key] == value


@pytest.mark.parametrize("patch", [
    {"start_time": "24:00:00"}, {"time": "23:60:00"},
    {"fallback_time": "10:0:00"}, {"start_time": "10:31:00", "time": "10:30:00"},
    {"start_time": "10:46:00", "fallback_time": "10:45:00"},
    {"time": "10:30:00", "fallback_time": "10:30:00"},
])
def test_only_present_clocks_are_compared(patch):
    with pytest.raises(ValueError):
        parameters(optional(**patch))


@pytest.mark.parametrize("key,price,other", [
    ("rise_pct", 10.6, 9.4), ("fall_pct", 9.4, 10.6),
])
def test_independent_return_threshold_with_other_disabled(key, price, other):
    sig = optional(**{key: 6})
    assert run(frame(price), sig=sig).hits[0]["reason"] == key.removesuffix("_pct")
    assert not run(frame(other), sig=sig).hits


def test_decline_only_needs_four_continuous_prices_without_volume_or_previous_close():
    data = window([10, 10, 10, 9.8])
    sig = optional(decline_3m_pct=2)
    assert not run(data, sig=sig, prev_close={}, now=AT + timedelta(minutes=2)).hits
    hit, = run(data, sig=sig, prev_close={}, now=AT + timedelta(minutes=3)).hits
    assert hit["reason"] == "decline_3m" and hit["trigger_time"] == (AT + timedelta(minutes=3)).isoformat()
    assert hit["decline_3m_pct"] == pytest.approx(2)
    assert not run(data.filter(pl.col("datetime") != AT + timedelta(minutes=1)),
                   sig=sig, prev_close={}, now=AT + timedelta(minutes=3)).hits


def test_volume_only_needs_thirteen_continuous_volumes_without_price_or_previous_close():
    data = window(volumes=[10] * 10 + [20] * 3)
    sig = optional(volume_ratio_min=2)
    assert not run(data, sig=sig, prev_close={}, now=AT + timedelta(minutes=11)).hits
    hit, = run(data, sig=sig, prev_close={}, now=AT + timedelta(minutes=12)).hits
    assert hit["reason"] == "volume_ratio" and hit["volume_ratio"] == 2
    assert not run(window(volumes=[0] * 10 + [20] * 3), sig=sig, prev_close={},
                   now=AT + timedelta(minutes=12)).hits


def test_enabled_volume_and_decline_are_conjunctive_but_alternative_to_returns():
    sig = optional(decline_3m_pct=2, volume_ratio_min=2)
    end = AT + timedelta(minutes=12)
    assert not run(window([10] * 13, [10] * 10 + [20] * 3), sig=sig, now=end).hits
    assert not run(window([10] * 12 + [9.8], [10] * 13), sig=sig, now=end).hits
    hit, = run(window([10] * 12 + [9.8], [10] * 10 + [20] * 3), sig=sig,
               prev_close={}, now=end).hits
    assert hit["reason"] == "volume_decline"
    assert run(frame(10.6), sig=optional(rise_pct=6, decline_3m_pct=2, volume_ratio_min=2)).hits


@pytest.mark.parametrize("kind", ["missing", "bad", "off_minute"])
def test_volume_dependency_cannot_bridge_bad_minutes(kind):
    data = window(volumes=[10] * 10 + [20] * 3)
    if kind == "missing":
        data = data.filter(pl.col("datetime") != AT + timedelta(minutes=5))
    elif kind == "bad":
        data = data.with_columns(pl.when(pl.col("datetime") == AT + timedelta(minutes=5))
                                 .then(float("nan")).otherwise(pl.col("volume")).alias("volume"))
    else:
        data = data.with_columns(pl.when(pl.col("datetime") == AT + timedelta(minutes=5))
                                 .then(pl.col("datetime") + timedelta(seconds=1))
                                 .otherwise(pl.col("datetime")).alias("datetime"))
    assert not run(data, sig=optional(volume_ratio_min=2), prev_close={},
                   now=AT + timedelta(minutes=12)).hits


def test_duplicate_is_normalized_without_counting_twice_and_future_is_ignored():
    data = window(volumes=[10] * 10 + [20] * 3)
    data = pl.concat([data, data.slice(5, 1), window(volumes=[999], first=AT + timedelta(minutes=13))])
    hit, = run(data, sig=optional(volume_ratio_min=2), prev_close={},
               now=AT + timedelta(minutes=12)).hits
    assert hit["volume_ratio"] == 2
    assert not run(data, sig=optional(volume_ratio_min=2), prev_close={},
                   now=AT + timedelta(minutes=11)).hits


def test_no_window_across_lunch_and_zero_recent_volume_is_valid():
    data = pl.concat([window(volumes=[10] * 10, first=AT.replace(hour=11, minute=21)),
                      window(volumes=[20] * 3, first=AT.replace(hour=13, minute=1))])
    assert not run(data, sig=optional(volume_ratio_min=2), prev_close={}, now=AT.replace(hour=13, minute=3)).hits
    assert not run(window(volumes=[10] * 10 + [0] * 3), sig=optional(volume_ratio_min=.1),
                   prev_close={}, now=AT + timedelta(minutes=12)).hits


def test_start_gate_uses_seconds_and_does_not_discard_lookback():
    sig = optional(start_time="10:03:01", decline_3m_pct=2)
    data = window([10, 10, 10, 9.8, 9.8])
    assert not run(data, sig=sig, prev_close={}, now=AT + timedelta(minutes=3)).hits
    hit, = run(data, sig=sig, prev_close={}, now=AT + timedelta(minutes=4)).hits
    assert hit["trigger_time"].endswith("10:04:00")
    assert not run(data, sig=sig, now=AT + timedelta(minutes=4),
                   positions=[position(entry_date=str(DAY))]).hits


@pytest.mark.parametrize("key,reason", [("time", "stop_time"), ("fallback_time", "fallback")])
def test_nullable_clocks_remain_independent_without_market_dependencies(key, reason):
    sig = optional(**{key: "10:30:15"})
    at = AT.replace(minute=30, second=15)
    assert not run(pl.DataFrame(), sig=sig, prev_close={}, now=at - timedelta(microseconds=1)).hits
    if key == "fallback_time":
        assert not run(pl.DataFrame(), sig=sig, prev_close={}, now=at).hits
        assert not run(pl.DataFrame(), sig=sig, prev_close={}, now=AT.replace(hour=15)).hits
        return
    hit, = run(pl.DataFrame(), sig=sig, prev_close={}, now=at).hits
    assert hit["reason"] == reason and hit["trigger_time"] == at.isoformat()
    assert run(pl.DataFrame(), sig=sig, prev_close={}, now=AT.replace(hour=15)).hits[0]["reason"] == reason


def test_signal_replay_does_not_restore_empty_clocks():
    class Repo:
        def get_daily_batch(self, *args, **kwargs):
            return pl.DataFrame()

        def get_minute_batch(self, *args, **kwargs):
            return window([10, 10, 10, 9.8])

    result = replay(Repo(), optional(decline_3m_pct=2), start=DAY, end=DAY,
                    symbols=["a"], positions=[position()])
    assert [(hit["reason"], hit["time"]) for hit in result["triggers"]] == [("decline_3m", "10:03:00")]
    assert not replay(Repo(), optional(), start=DAY, end=DAY, symbols=["a"], positions=[position()])["triggers"]
