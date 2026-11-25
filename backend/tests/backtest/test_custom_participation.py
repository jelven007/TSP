"""Separate entry/exit caps must constrain actual minute fills in every matcher."""
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import polars as pl
import pytest

from app.api.backtest import StrategyBacktestRequest, _make_job_key
from app.backtest.engine import BacktestEngine, MatcherConfig
from app.backtest.entry_execution import execute_entry_window
from app.backtest.exit_execution import execute_exit_window
from app.backtest.strategy import StrategyBacktestConfig

SYMBOL = "600001.SH"
DAY = date(2026, 9, 4)


def frame(day=DAY, start=571, count=5, volume=15.):
    return pl.DataFrame([
        {"symbol": SYMBOL, "datetime": datetime.combine(day, datetime.min.time())
         + timedelta(minutes=start + i), "open": 10., "high": 10.2,
         "low": 9.8, "close": 10., "volume": volume}
        for i in range(count)
    ])


def test_entry_cap_rounds_down_and_buys_each_minute_high():
    order = execute_entry_window(
        frame(), symbol=SYMBOL, start_at=frame()["datetime"][0], prior_close=10.,
        budget=1000000., size_order=lambda _: 1000, buy_value=lambda n: n,
        participation_pct=.1,
    )
    assert [f.shares for f in order.fills] == [100] * 5
    assert [f.price for f in order.fills] == [10.2] * 5
    assert all(f.participation <= .1 for f in order.fills)
    assert order.cancelled_shares == 500


def test_exit_has_independent_cap_low_prices_and_preserves_residual():
    order = execute_exit_window(
        frame(), symbol=SYMBOL, start_at=frame()["datetime"][0],
        reference_price=None, quantity=2000, sell_value=lambda n: n,
        participation_pct=.2, minute_price_rule="high_low_v1",
    )
    assert [f.shares for f in order.fills] == [300] * 5
    assert [f.price for f in order.fills] == [9.8] * 5
    assert order.cancelled_shares == 500
    assert all(f.participation <= .2 for f in order.fills)


@pytest.mark.parametrize("value", [0, -1, 1.01, float("nan"), float("inf")])
def test_invalid_rates_fail_at_api_and_frozen_config(value):
    with pytest.raises(ValueError):
        StrategyBacktestRequest(strategy_id="any_custom", entry_participation_pct=value)
    with pytest.raises(ValueError):
        StrategyBacktestConfig("any_custom", None, DAY, DAY, exit_participation_pct=value)


def test_new_request_defaults_and_independent_job_signatures():
    req = StrategyBacktestRequest(strategy_id="any_custom")
    assert req.entry_participation_pct == req.exit_participation_pct == .1
    args = ("any_custom", None, "2026-09-04", "2026-09-07", "open_t+1", "close_t",
            "close_t", .0002, 10., 4, 1., 1000000., "equal", None, None)
    keys = {_make_job_key(*args, entry_participation_pct=entry, exit_participation_pct=exit)
            for entry, exit in ((.1, .1), (.2, .1), (.1, .2))}
    assert len(keys) == 3
    old = StrategyBacktestConfig("any_custom", None, DAY, DAY)
    assert old.entry_participation_pct is old.exit_participation_pct is None


def test_scoped_stream_freezes_rates_and_cancels_the_matching_job(monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from app.api import backtest, strategy
    from app.config import settings
    from app.multitenancy import research

    jobs, cancelled = [], []
    service = SimpleNamespace(
        submit=lambda task, key: jobs.append((task, key)) or task,
        cancel_key=lambda key, kind: cancelled.append(key) or True,
    )
    monkeypatch.setattr(settings, "backtest_range_guard", False)
    monkeypatch.setattr(research, "jobs_for", lambda _: service)
    monkeypatch.setattr(research, "stream", lambda *args: JSONResponse({"ok": True}))
    monkeypatch.setattr(strategy, "_get_engine", lambda _: SimpleNamespace(
        task=lambda kind, config: {"kind": kind, "config": config}))
    app = FastAPI()
    app.include_router(backtest.router)
    query = {"strategy_id": "custom_unrelated", "start": "2026-09-04", "end": "2026-09-07",
             "entry_participation_pct": .2, "exit_participation_pct": .05}
    with TestClient(app) as client:
        assert client.get("/api/backtest/strategy/stream", params=query).status_code == 200
        config = jobs[0][0]["config"]
        assert config["entry_participation_pct"] == .2
        assert config["exit_participation_pct"] == .05
        assert client.post("/api/backtest/strategy/cancel", json={"qs": urlencode(query)}).status_code == 200
        assert cancelled == [jobs[0][1]]


def test_arbitrary_minute_custom_without_exit_template_uses_cap_on_holding_exit(tmp_path):
    from test_minute_backtest import _daily_panel, _make_service, _minute_frame, _trading_days
    days = _trading_days(49, start=date(2026, 7, 1))
    panel = _daily_panel(days, [SYMBOL]).with_columns(
        pl.lit(10.).alias("close"), pl.lit(10.).alias("raw_close"))
    buy, sell = days[-2:]
    times = [(SYMBOL, f"14:{minute:02d}", 10.6) for minute in range(30, 37)]
    minutes = {buy: _minute_frame(buy, times).with_columns(pl.lit(20.).alias("volume")),
               sell: pl.concat([frame(sell, start=900, count=1, volume=20.),
                                frame(sell, volume=20.)])}
    service = _make_service(tmp_path, panel, minutes)
    result = service.run(StrategyBacktestConfig(
        "test_minute_ping", [SYMBOL], buy, sell, initial_capital=1000000,
        overrides={"basic_filter": {"enabled": False}, "max_hold_days": 1},
        exit_fill="signal_next_minute", entry_participation_pct=.1,
        exit_participation_pct=.05, minute_price_rule="high_low_v1",
    ))
    assert not result.error, result.error
    buys = [f for f in result.fills if f["side"] == "buy"]
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert buys and sells
    assert all(f["participation"] <= .1 for f in buys)
    assert all(f["participation"] <= .05 and f["price"] == 9.8 for f in sells)


def test_daily_custom_service_keeps_first_day_prior_close_for_capacity(tmp_path):
    from test_strategy_backtest_correctness import _EngineStub, _strategy, _StrategyEngineStub

    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestService
    days = [DAY + timedelta(days=i) for i in range(-1, 3)]
    daily = pl.DataFrame([
        {"symbol": SYMBOL, "date": day, "name": "test", "open": 10.,
         "high": 10.2, "low": 9.8, "close": 10., "raw_close": 10.,
         "volume": 10000., "amount": 100000., "score": 1.,
         "signal_limit_up": False, "signal_limit_down": False}
        for day in days
    ])
    engine = _EngineStub(daily, tmp_path)
    engine.repo.get_minute_by_dates = lambda symbols, dates, asset_type: pl.concat([
        frame(day, start=900, count=1, volume=100.) for day in dates])
    actual = BacktestEngine(engine.repo)
    engine.simulate_market_matrix = actual.simulate_market_matrix
    service = StrategyBacktestService(engine, _StrategyEngineStub(_strategy()))
    result = service.run(StrategyBacktestConfig(
        "test", [SYMBOL], DAY, days[-1], entry_fill="close_t", exit_fill="close_t",
        initial_capital=1000000, entry_participation_pct=.1, exit_participation_pct=.05,
        minute_price_rule="high_low_v1", overrides={"max_hold_days": 1},
    ), result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert not result.error, result.error
    buys = [f for f in result.fills if f["side"] == "buy"]
    assert buys and buys[0]["date"] == str(DAY)


def test_first_session_reference_has_identical_panel_and_native_matrix_results():
    from app.backtest.matrix import build_market_data_matrix
    from app.backtest.strategy import _first_entry_prior_closes
    daily = pl.DataFrame({
        "date": [DAY - timedelta(days=1), DAY], "symbol": [SYMBOL] * 2,
        "open": [10.] * 2, "high": [10.2] * 2, "low": [9.8] * 2,
        "close": [5.] * 2, "raw_close": [10.] * 2, "volume": [100.] * 2,
    })
    matrix = build_market_data_matrix(daily, field_columns={"raw_close"})
    expected = {(SYMBOL, str(DAY)): 10.}
    assert _first_entry_prior_closes(daily, DAY) == expected
    assert _first_entry_prior_closes(matrix, DAY) == expected


@pytest.mark.parametrize("mode", ["portfolio", "independent"])
@pytest.mark.parametrize("entry_fill,exit_fill", [
    ("close_t", "close_t"), ("open_t+1", "open_t+1"),
])
def test_generic_daily_custom_matching_caps_both_sides_including_end(mode, entry_fill, exit_fill):
    days = [DAY + timedelta(days=i) for i in range(-1, 4)]
    daily = pl.DataFrame([
        {"symbol": SYMBOL, "date": day, "open": 10., "high": 10.2, "low": 9.8,
         "close": 10., "raw_close": 10., "volume": 10000., "score": 1.,
         "signal_limit_up": False, "signal_limit_down": False}
        for day in days
    ])
    minutes = pl.concat([pl.concat([
        frame(day, volume=100.), frame(day, start=900, count=1, volume=100.),
    ]) for day in days])

    class Repo:
        def get_minute_by_dates(self, symbols, dates, asset_type):
            return minutes.filter(pl.col("datetime").dt.date().is_in(dates))

    engine = BacktestEngine(Repo())
    config = MatcherConfig(
        entry_fill=entry_fill, exit_fill=exit_fill, initial_capital=1000000.,
        max_positions=1, fees_pct=0., slippage_bps=0.,
        entry_capacity_window=True, minute_price_rule="high_low_v1",
        entry_participation_pct=.1, exit_participation_pct=.05,
    )
    run = engine.simulate_portfolio if mode == "portfolio" else engine.simulate_independent_candidates
    result = run(daily, pl.Series([False, True, False, False, False]),
                 pl.Series([False, False, False, True, False]), config)
    assert "error" not in result.stats, result.stats
    buys = [f for f in result.fills if f["side"] == "buy"]
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert buys and sells
    assert all(f["participation"] <= .1 and f["price"] == 10.2 for f in buys)
    assert all(f["participation"] <= .05 and f["price"] == 9.8 for f in sells)
    # Every real minute has a single capacity budget, even at final liquidation.
    assert len({(f["datetime"], f["side"]) for f in result.fills}) == len(result.fills)
