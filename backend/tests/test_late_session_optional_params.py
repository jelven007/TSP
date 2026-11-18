"""Optional late-session conditions must disable both gates and dependencies."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.strategy.late_session_strength import DEFAULT_PARAMS, bar_time, evaluate, parameters

DAY = date(2026, 9, 4)
AT = datetime(2026, 9, 4, 14, 30)


def signal(**patch):
    return {"template": "late_session_strength", "enabled": True,
            "params": {key: None for key in DEFAULT_PARAMS} | {
                "amount_min_100m": None, "turnover_min_pct": None,
                "amplitude_max_pct": None, "volume_ratio_min": None, "end_time": None,
            } | patch}


def bars():
    return pl.DataFrame({
        "symbol": ["a", "a"], "datetime": [AT - timedelta(hours=4), AT],
        "close": [10.0, 10.1], "high": [10.2, 10.1], "low": [9.8, 10.0],
        "amount": [5e7, 5e7], "volume": [5000.0, 5000.0],
    })


def run(sig, frame=None, **kwargs):
    previous = kwargs.pop("prev_close", {})
    return evaluate(bars() if frame is None else frame, sig, day=DAY,
                    pool_symbols=["a"], prev_close=previous, completed_through=AT, **kwargs)


def test_all_empty_conditions_need_only_a_valid_completed_price():
    frame = bars().select("symbol", "datetime", "close")
    result = run(signal(), frame)
    assert result.symbols == ["a"] and not result.diagnostics
    assert result.progress["steps"]["breadth"]["state"] == "skipped"
    assert result.progress["steps"]["ma1"]["state"] == "skipped"
    assert bar_time(signal(), DAY, completed_through=AT) == AT


@pytest.mark.parametrize("key", [
    "pool_up_min_pct", "amount_min_100m", "turnover_min_pct",
    "amplitude_max_pct", "change_min_pct", "change_max_pct", "volume_ratio_min",
])
def test_new_numeric_contract_includes_bounds_and_rejects_invalid_values(key):
    for value in (.1, 100):
        assert parameters(signal(**{key: value}))[key] == value
    for value in (0, .099, 100.01, True, float("nan")):
        with pytest.raises(ValueError):
            parameters(signal(**{key: value}))


def test_inclusive_amount_uses_morning_and_deduplicates_without_future_amount():
    original = bars()
    duplicate = original.tail(1)
    future = original.tail(1).with_columns(
        pl.lit(AT + timedelta(minutes=1)).alias("datetime"), pl.lit(1e10).alias("amount"))
    frame = pl.concat([original, duplicate, future])
    assert run(signal(amount_min_100m=1), frame).symbols == ["a"]
    assert run(signal(amount_min_100m=1.1), frame).symbols == []


def test_enabled_dependencies_do_not_pass_when_missing():
    for key in ("turnover_min_pct", "amplitude_max_pct", "volume_ratio_min", "change_min_pct"):
        assert run(signal(**{key: .1})).symbols == []
    assert run(signal(turnover_min_pct=1), float_shares={"a": 1e8}).symbols == ["a"]
    assert run(signal(turnover_min_pct=1.1), float_shares={"a": 1e8}).symbols == []
    assert run(signal(amplitude_max_pct=4), prev_close={"a": 10}).symbols == ["a"]


def test_dynamic_delay_and_end_boundary_do_not_restore_default_start():
    frame = bars()
    assert run(signal(evaluation_delay_minutes=.5), frame).stage == "waiting_confirmation"
    result = evaluate(frame, signal(evaluation_delay_minutes=.5, end_time="14:30:30"),
                      day=DAY, pool_symbols=["a"], prev_close={},
                      completed_through=AT + timedelta(seconds=30))
    assert result.symbols == ["a"]
    assert run(signal(end_time="14:29:59"), frame).symbols == []


def test_ma_count_one_checks_one_ma_value_and_does_not_require_disabled_period():
    frame = pl.DataFrame({
        "symbol": ["a"] * 5, "datetime": [AT - timedelta(minutes=i) for i in range(4, -1, -1)],
        "close": [10.0] * 5,
    })
    assert run(signal(ma5_1m_count=1), frame).symbols == ["a"]
    assert run(signal(ma5_1m_count=2), frame).symbols == []
    assert parameters(signal(ma5_5m_count=100))["ma5_5m_count"] == 100
    with pytest.raises(ValueError):
        parameters(signal(ma5_1m_count=101))


def test_five_session_volume_reference_never_uses_today_or_skips_missing_day():
    from app.strategy.basic_filter import previous_session
    from app.strategy.late_session_strength import condition_inputs

    days = []
    prior = DAY
    for _ in range(5):
        prior = previous_session(prior)
        days.append(prior)
    daily = pl.DataFrame({"symbol": ["a"] * 6, "date": [*days, DAY],
                          "volume": [10000.0] * 5 + [1e10]})
    context = SimpleNamespace(as_of=DAY, daily_history=daily, current=None, pool_history=None,
                              pool_universe=None)
    inputs = condition_inputs(context, signal(volume_ratio_min=1))
    assert inputs["previous_volume_mean"] == {"a": 10000}
    assert run(signal(volume_ratio_min=1), **inputs).symbols == ["a"]
    assert run(signal(volume_ratio_min=1.2), **inputs).symbols == []
    context.daily_history = daily.filter(pl.col("date") != days[2])
    assert condition_inputs(context, signal(volume_ratio_min=1))["previous_volume_mean"] == {}


def test_api_roundtrips_all_empty_params_and_rejects_before_overwriting(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router

    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), clear_cache=lambda: None)
    app.include_router(router)
    client = TestClient(app)
    payload = {"id": "optional_late", "name": "可选尾盘", "kind": "entry", "conditions": [],
               "timeframe": "intraday", **signal()}
    saved = client.post("/api/custom-signals", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["signal"]["params"] == payload["params"]
    invalid = client.post("/api/custom-signals", json={**payload, "params": {**payload["params"], "amount_min_100m": 0}})
    assert invalid.status_code == 400
    assert client.get("/api/custom-signals").json()["signals"][0]["params"] == payload["params"]


def test_engine_empty_conditions_skip_previous_close_and_pulse_on_latest_completed_bar(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    sig = {"id": "optional_late", "name": "可选尾盘", "kind": "entry",
           "conditions": [], "timeframe": "intraday", **signal()}
    custom_signals.save_one(tmp_path, sig)
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    frame = bars().select("symbol", "datetime", "close")
    context = StrategyDataContext("stock", "1m", DAY, history=frame, observed_at=AT)
    result = engine._inject_intraday_signal_columns(frame, context=context, pool_symbols=["a"])
    assert result.filter(pl.col("csgi_optional_late"))["datetime"].to_list() == [AT]
    assert context.reference_errors == {}


def test_replay_empty_start_checks_each_bar_and_respects_exact_end_without_loading_daily(tmp_path):
    from app.services.daily_stock_pools import DailyStockPools
    from app.services.late_session_replay import replay
    from app.services.strategy_results import ResultIdentity

    identity = ResultIdentity("s", "示例", "v1", "{}", "{}", "null", "stock", "1m")
    DailyStockPools(tmp_path).save(identity, {
        "strategy_id": "s", "as_of": str(DAY), "symbols": ["a"], "total": 1,
    }, started_at=1)
    minute = bars().tail(1)
    minute = pl.concat([minute, minute.with_columns(pl.lit(AT + timedelta(minutes=1)).alias("datetime"))])
    repo = SimpleNamespace(get_minute_batch=lambda *args, **kwargs: minute)
    result = replay(repo, tmp_path, {"id": "optional", **signal(end_time="14:30:00")},
                    strategy_id="s", start=DAY, end=DAY, symbols=["a"])
    assert result["triggers"] == [{"date": str(DAY), "time": "14:30:00", "symbol": "a"}]


def test_empty_start_keeps_saved_delay_pending_against_completed_data(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine
    from app.strategy.late_session_strength import pending_evaluation_time

    sig = {"id": "optional_late", "name": "可选尾盘", "kind": "entry", "conditions": [],
           "timeframe": "intraday", **signal(evaluation_delay_minutes=.5)}
    custom_signals.save_one(tmp_path, sig)
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    strategy = SimpleNamespace(entry_signals=["csgi_optional_late"])
    context = StrategyDataContext("stock", "1m", DAY, history=bars(), observed_at=AT)
    assert pending_evaluation_time(engine, strategy, {}, context) == AT + timedelta(seconds=30)
    context.observed_at = AT + timedelta(seconds=30)
    assert pending_evaluation_time(engine, strategy, {}, context) is None


def test_five_minute_ma_is_independent_and_morning_windows_do_not_cross_sessions():
    minute = pl.DataFrame({
        "symbol": ["a"] * 25, "datetime": [AT - timedelta(minutes=i) for i in range(24, -1, -1)],
        "close": [10.0] * 25, "open": [10.0] * 25, "high": [10.0] * 25,
        "low": [10.0] * 25, "volume": [1.0] * 25, "amount": [1000.0] * 25,
    })
    assert run(signal(ma5_5m_count=1), minute).symbols == ["a"]
    early = minute.tail(5).with_columns((pl.col("datetime") - timedelta(hours=4)).alias("datetime"))
    result = evaluate(early, signal(ma5_1m_count=1), day=DAY, pool_symbols=["a"], prev_close={},
                      completed_through=AT - timedelta(hours=4))
    assert result.symbols == ["a"]
