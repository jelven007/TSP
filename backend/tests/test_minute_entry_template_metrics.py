"""Shared four optional entry thresholds: ranges, inputs and persisted values."""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.strategy.late_session_strength import DEFAULT_PARAMS, evaluate, parameters

DAY = date(2026, 9, 4)
AT = datetime.combine(DAY, time(14, 30))
BOUNDS = {
    "return_5m_min_pct": -10,
    "return_15m_min_pct": -10,
    "position_min_pct": .1,
    "gap_max_pct": .1,
}


def signal(**patch):
    return {"id": "shared_entry", "name": "分钟入场", "kind": "entry", "enabled": True,
            "template": "late_session_strength", "timeframe": "intraday", "conditions": [],
            "params": dict.fromkeys(DEFAULT_PARAMS) | dict.fromkeys(BOUNDS) | patch}


def frame():
    return pl.DataFrame({
        "symbol": ["a"] * 4,
        "datetime": [datetime.combine(DAY, time(9, 31)), AT - timedelta(minutes=15),
                     AT - timedelta(minutes=5), AT],
        "open": [10.01, 10., 10., 10.],
        "high": [10.5] * 4, "low": [9.5] * 4, "close": [10., 10., 10., 10.1],
    })


def run(**patch):
    bars = patch.pop("bars", frame())
    symbols = patch.pop("symbols", ["a"])
    return evaluate(bars, signal(**patch), day=DAY, pool_symbols=symbols,
                    prev_close={"a": 10, "b": 10}, completed_through=AT)


@pytest.mark.parametrize("key,minimum", BOUNDS.items())
def test_bounds_and_explicit_null(key, minimum):
    for value in (None, minimum, 100):
        assert parameters(signal(**{key: value}))[key] == value
    for value in (minimum - .01, 100.01, True, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            parameters(signal(**{key: value}))


@pytest.mark.parametrize("key,threshold", [
    ("return_5m_min_pct", 1), ("return_15m_min_pct", 1),
    ("position_min_pct", 60), ("gap_max_pct", .1),
])
def test_inclusive_thresholds_and_rejection(key, threshold):
    assert run(**{key: threshold}).symbols == ["a"]
    rejected = threshold - .01 if key == "gap_max_pct" else threshold + .01
    # Gap lower bound is .1; test a wider actual opening gap instead.
    bars = frame()
    if key == "gap_max_pct":
        bars = bars.with_columns(pl.when(pl.col("datetime").dt.time() == time(9, 31))
            .then(pl.lit(10.02)).otherwise(pl.col("open")).alias("open"))
        rejected = .1
    assert run(bars=bars, **{key: rejected}).symbols == []


def test_deduplicates_last_bar_and_ignores_future_prices():
    bars = pl.concat([
        frame().with_columns(pl.when(pl.col("datetime") == AT).then(pl.lit(50.))
                             .otherwise(pl.col("close")).alias("close")),
        frame().tail(1),
        frame().tail(1).with_columns(pl.lit(AT + timedelta(minutes=1)).alias("datetime"),
                                    pl.lit(100.).alias("high"), pl.lit(1.).alias("low")),
    ])
    assert run(bars=bars, return_5m_min_pct=1, return_15m_min_pct=1,
               position_min_pct=60, gap_max_pct=.1).symbols == ["a"]


def test_disabled_metrics_need_only_close():
    assert run(bars=frame().tail(1).select("symbol", "datetime", "close")).symbols == ["a"]


def test_missing_required_minute_isolates_stock_and_all_missing_fails():
    missing = frame().filter(pl.col("datetime") != AT - timedelta(minutes=5))
    assert run(bars=missing, return_5m_min_pct=0).diagnostics
    combined = pl.concat([missing, frame().with_columns(pl.lit("b").alias("symbol"))])
    result = run(bars=combined, symbols=["a", "b"], return_5m_min_pct=0)
    assert result.symbols == ["b"] and not result.diagnostics
    assert result.progress["steps"]["change"]["missing"] == 1


@pytest.mark.parametrize("key", ["position_min_pct", "gap_max_pct"])
def test_missing_ohlc_or_opening_does_not_pass(key):
    bars = frame().drop("high") if key == "position_min_pct" else frame().slice(1)
    assert run(bars=bars, **{key: .1}).diagnostics


def test_api_roundtrip_and_invalid_save_preserves_previous_values(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router

    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), clear_cache=lambda: None)
    app.include_router(router)
    client = TestClient(app)
    payload = signal(return_5m_min_pct=-10, return_15m_min_pct=-.15,
                     position_min_pct=.1, gap_max_pct=100)
    assert client.post("/api/custom-signals", json=payload).status_code == 200
    saved = client.get("/api/custom-signals").json()["signals"][0]
    assert all(saved["params"][key] == payload["params"][key] for key in BOUNDS)
    invalid = {**payload, "params": {**payload["params"], "return_5m_min_pct": -10.01}}
    assert client.post("/api/custom-signals", json=invalid).status_code == 400
    assert client.get("/api/custom-signals").json()["signals"][0] == saved


def test_engine_loads_previous_close_when_only_gap_is_enabled(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.basic_filter import previous_session
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    saved = signal(gap_max_pct=.1)
    custom_signals.save_one(tmp_path, saved)
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    previous = pl.DataFrame({"symbol": ["a"], "date": [previous_session(DAY)],
                             "close": [10.], "raw_close": [10.]})
    context = StrategyDataContext("stock", "1m", DAY, history=frame(), daily_history=previous,
                                  observed_at=AT)
    injected = engine._inject_intraday_signal_columns(frame(), context=context, pool_symbols=["a"])
    assert injected.filter(pl.col("csgi_shared_entry"))["datetime"].to_list() == [AT]
