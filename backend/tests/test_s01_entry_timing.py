"""The editable S01 clock must control evaluation and preserve frozen legacy signals."""
from datetime import datetime

import polars as pl
import pytest
from test_s01_optional_triggers import context, optional
from test_s01_parameter_signals import ENTRY, EXIT
from test_s01_strategy import BUILTINS, _service

from app.strategy import custom_signals
from app.strategy.engine import StrategyEngine


def timed(**values):
    return {**ENTRY, "params": {**ENTRY["params"], "time": "14:45:00",
                               "evaluation_delay_minutes": 1, **values}}


def test_new_complete_clock_validates_without_mutating_legacy():
    from app.strategy.s01_signals import signal_parameters

    custom_signals.validate(timed())
    assert signal_parameters(timed())["evaluation_delay_minutes"] == 1
    assert signal_parameters(ENTRY) == ENTRY["params"]


@pytest.mark.parametrize("values", [
    {"time": "14:45"}, {"time": "12:45:00"}, {"time": "14:59:30"},
    {"evaluation_delay_minutes": True}, {"evaluation_delay_minutes": 0},
    {"evaluation_delay_minutes": .75}, {"evaluation_delay_minutes": 16},
])
def test_invalid_clock_is_rejected(values):
    with pytest.raises(ValueError):
        custom_signals.validate(timed(**values))


def test_environment_starts_before_stock_confirmation_and_never_uses_future_bars():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=890)
    ctx.history = ctx.history.with_columns(
        pl.when(pl.col("datetime") > datetime(2026, 8, 25, 14, 45))
        .then(100.).otherwise(pl.col("close")).alias("close"))
    with custom_signals.private_signal_registry([timed(), EXIT]):
        ctx.observed_at = datetime(2026, 8, 25, 14, 44, 59)
        assert e.run("s01", ctx).total == 0
        ctx.observed_at = datetime(2026, 8, 25, 14, 45)
        environment = e.run("s01", ctx)
        assert environment.total == 0
        quality = environment.entry_data_quality["csgi_" + ENTRY["id"]]
        assert quality["market_valid_count"] == 1
        assert quality["market_positive_count"] == 1
        assert quality["stage"] == "waiting_confirmation"
        ctx.observed_at = datetime(2026, 8, 25, 14, 45, 59)
        assert e.run("s01", ctx).total == 0
        ctx.observed_at = datetime(2026, 8, 25, 14, 46)
        result = e.run("s01", ctx)
        assert result.total == 1
        assert result.rows[0]["close"] == 10.25
        assert result.rows[0]["last_datetime"] == datetime(2026, 8, 25, 14, 45)
        assert result.rows[0]["entry_evaluated_at"] == datetime(2026, 8, 25, 14, 46)


def test_timed_native_and_other_entry_keep_independent_or_semantics():
    from test_s01_optional_triggers import DAILY_ENTRY

    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=890)
    ctx.observed_at = datetime(2026, 8, 25, 14, 46)
    sig = timed(current_return_max=.021)
    with custom_signals.private_signal_registry([sig, EXIT, DAILY_ENTRY]):
        result = e.run("s01", ctx, overrides={"entry_signals": [
            "csgi_" + ENTRY["id"], "csg_daily_entry"]})
    assert result.entry_signal_hits == [{"symbol": "600001.SH", "signals": ["csg_daily_entry"]}]


def test_next_pulse_visits_start_then_confirmation(tmp_path):
    from app.strategy.s01_signals import next_entry_evaluation_time

    e = StrategyEngine([BUILTINS])
    s = optional(e)
    e.documents = tmp_path
    ctx = context(end=890)
    with custom_signals.private_signal_registry([timed(), EXIT]):
        for now, expected in [("14:44:59", "14:45:00"), ("14:45:00", "14:46:00"),
                              ("14:45:59", "14:46:00"), ("14:46:00", None)]:
            ctx.observed_at = datetime.fromisoformat("2026-08-25T" + now)
            pending = next_entry_evaluation_time(e, s, {}, ctx)
            assert (pending.time().isoformat() if pending else None) == expected


def test_changed_second_clock_uses_its_completed_start_minute_and_saved_values():
    e = StrategyEngine([BUILTINS])
    optional(e)
    ctx = context(end=884)
    sig = timed(time="14:44:20", evaluation_delay_minutes=.5)
    with custom_signals.private_signal_registry([sig, EXIT]):
        ctx.observed_at = datetime(2026, 8, 25, 14, 44, 49)
        assert e.run("s01", ctx).total == 0
        ctx.observed_at = datetime(2026, 8, 25, 14, 44, 50)
        result = e.run("s01", ctx, params={"time": "14:45:00", "evaluation_delay_minutes": 10})
        assert result.total == 1
        assert result.rows[0]["last_datetime"] == datetime(2026, 8, 25, 14, 44)
        ctx.completed_through = datetime(2026, 8, 25, 14, 43)
        assert e.run("s01", ctx).total == 0


@pytest.mark.parametrize("start,delay,expected", [
    ("14:45:00", 1, [887, 888, 889, 890, 891]),
    ("14:44:20", .5, [885, 886, 887, 888, 889]),
])
def test_replay_buy_window_follows_confirmation(tmp_path, start, delay, expected):
    from datetime import date

    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    original = service.engine.repo.get_minute_by_dates

    def small_capacity(*args, **kwargs):
        return original(*args, **kwargs).with_columns(
            pl.when(pl.col("datetime").dt.hour() == 14)
            .then(1.).otherwise(pl.col("volume")).alias("volume"))

    service.engine.repo.get_minute_by_dates = small_capacity
    optional(service.strategy_engine)
    sig = timed(time=start, evaluation_delay_minutes=delay, current_return_min=0)
    with custom_signals.private_signal_registry([sig, EXIT]):
        result = service.run(StrategyBacktestConfig(
            "s01", None, date(2026, 8, 25), date(2026, 8, 26),
            params={"initial_cash": 1000000, "participation": .001}),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    assert sorted({f["bar"] for f in result.fills if f["side"] == "buy"}) == expected
    assert result.config["parameter_signals"]["entry"] == sig


def test_api_persists_seconds_contract_and_invalid_updates_do_not_write(tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.signals import router

    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(router)
    client = TestClient(app)
    for seconds in (30, 60, 120, 900):
        request = timed(evaluation_delay_minutes=seconds / 60)
        response = client.post("/api/custom-signals", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["signal"]["params"] == request["params"]
    before = client.get("/api/custom-signals").json()
    assert client.post("/api/custom-signals", json=timed(evaluation_delay_minutes=.75)).status_code == 400
    assert client.get("/api/custom-signals").json() == before
