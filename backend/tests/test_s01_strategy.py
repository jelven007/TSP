from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext, StrategyEngine

BUILTINS = Path(__file__).resolve().parents[1] / "app/strategy/builtin"


def bars(day=date(2026, 8, 25), end=885, symbol="600001.SH", price=10.1):
    labels = list(range(571, min(end, 690) + 1))
    if end >= 781:
        labels += list(range(781, end + 1))
    return pl.DataFrame({
        "symbol": [symbol] * len(labels),
        "datetime": [datetime.combine(day, datetime.min.time()) + timedelta(minutes=t) for t in labels],
        "open": [price] * len(labels), "high": [price + .01] * len(labels),
        "low": [price - .01] * len(labels), "close": [price] * len(labels),
        "volume": [1000.] * len(labels), "amount": [price * 100000] * len(labels),
    })


def daily():
    return pl.DataFrame({
        "date": [date(2026, 8, 21), date(2026, 8, 24)],
        "symbol": ["600001.SH"] * 2, "name": ["正常股票"] * 2,
        "close": [9.9, 10.], "raw_close": [9.9, 10.],
        "high": [10., 10.1], "raw_high": [10., 10.1],
        "low": [9.8, 9.9], "raw_low": [9.8, 9.9],
        "amount": [1e8] * 2, "listing_age_days": [365] * 2,
        "is_risk_warning": [False] * 2,
    })


def test_s01_registered_and_causal_prefix():
    engine = StrategyEngine(strategy_dirs=[BUILTINS])
    assert engine.has("s01"), engine.load_errors()
    context = StrategyDataContext("stock", "1m", date(2026, 8, 25),
                                  current=daily().tail(1), history=bars(end=890),
                                  daily_history=daily())
    result = engine.run("s01", context, params={"market_min_valid": 1})
    assert [x["symbol"] for x in result.rows] == ["600001.SH"]
    assert result.rows[0]["last_datetime"].strftime("%H:%M") == "14:45"
    context.history = bars(end=884)
    assert engine.run("s01", context, params={"market_min_valid": 1}).total == 0


def test_listing_age_parameter_controls_the_shared_engine_filter():
    engine = StrategyEngine([BUILTINS])
    history = daily().with_columns(pl.lit(40).alias("listing_age_days"))
    context = StrategyDataContext("stock", "1m", date(2026, 8, 25),
                                  current=history.tail(1), history=bars(), daily_history=history)
    assert engine.run("s01", context, params={"market_min_valid": 1}).total == 0
    assert engine.run("s01", context, params={"market_min_valid": 1, "listing_days": 30}).total == 1


def test_morning_short_windows_guard_and_persistence():
    from app.strategy.builtin._overnight import exit_signal

    frame = bars(end=689, price=10.)
    # Morning VWAP loss, 15-minute decline.
    frame = frame.with_columns(pl.when(pl.col("datetime").dt.minute() == 15)
                               .then(9.98).otherwise(pl.col("close")).alias("close"))
    signal = exit_signal(frame, 675, 10., {})
    assert signal["reason"] == "morning_vwap_or_trailing"
    assert signal["first_eligible_bar"] == 676
    assert exit_signal(bars(end=689, price=11.), 683, 10., {}, pending=signal) == signal
    assert exit_signal(pl.DataFrame(), 682, None, {})["reason"] == "time_guard"

    short = bars(end=681, price=10.).with_columns(
        pl.when(pl.col("datetime") >= datetime(2026, 8, 25, 11, 19))
        .then(9.96).otherwise(pl.col("close")).alias("close"),
        pl.when(pl.col("datetime") >= datetime(2026, 8, 25, 11, 19))
        .then(1500.).otherwise(pl.col("volume")).alias("volume"),
    )
    short_signal = exit_signal(short, 681, None, {})
    assert short_signal["reason"] == "volume_decline"
    assert short_signal["volume_ratio_3m"] == pytest.approx(1.5)
    missing = short.filter(pl.col("datetime") != datetime(2026, 8, 25, 11, 9))
    assert exit_signal(missing, 681, None, {}) is None


def test_s01_parameters_persist(tmp_path):
    from app.strategy.config import load_override, save_override

    engine = StrategyEngine(strategy_dirs=[BUILTINS])
    assert engine.has("s01")
    values = {"params": {"participation": .075, "entry_budget": 20000,
                         "volume_ratio_min": 1.5, "guard_time": "11:22"}}
    save_override(tmp_path, "s01", values)
    assert load_override(tmp_path, "s01") == values
    assert engine.resolve_params(engine.get("s01"), None, values)["participation"] == .075


def test_capacity_partial_fills_and_aggregated_fees():
    from app.backtest.overnight import fill_order

    source = bars(end=889).filter(pl.col("datetime") >= datetime(2026, 8, 25, 14, 47))
    source = source.with_columns(pl.lit(40.).alias("volume"))  # 4000股, 7.5%=300股
    result = fill_order(source, "buy", "600001.SH", 10., 887, 889, {"participation": .075}, budget=20000.)
    assert [r["shares"] for r in result["fills"]] == [300, 300, 300]
    assert result["shares"] == 900
    assert result["value"] <= 20000
    assert result["fee"] == pytest.approx(5 + result["notional"] * .00001)
    sell = fill_order(source, "sell", "600001.SH", 10., 887, 889, {"participation": .075}, quantity=1000)
    assert sell["remaining"] == 100
    assert sell["fee"] == pytest.approx(5 + sell["notional"] * .00051)
    assert max(f["participation"] for f in sell["fills"]) <= .075


def test_capacity_star_odd_remainder_and_zero_volume():
    from app.backtest.overnight import fill_order

    source = bars(end=689, symbol="688001.SH").filter(
        pl.col("datetime") == datetime(2026, 8, 25, 11, 16))
    source = source.with_columns(pl.lit(40.).alias("volume"))
    result = fill_order(source, "sell", "688001.SH", 10., 676, 689, {}, quantity=199)
    assert result["remaining"] == 0
    assert result["shares"] == 199
    source = source.with_columns(pl.lit(0.).alias("volume"))
    assert fill_order(source, "sell", "688001.SH", 10., 676, 689, {}, quantity=199)["shares"] == 0


def test_engine_sell_is_independent_of_candidates_and_t1():
    engine = StrategyEngine([BUILTINS])
    context = StrategyDataContext("stock", "1m", date(2026, 8, 25),
                                  current=daily().tail(1), history=bars(end=689),
                                  daily_history=daily(),
                                  positions=[{"symbol": "600001.SH", "qty": 1000,
                                              "available_qty": 1000, "entry_unit_cost": 10.1,
                                              "sell_date": "2026-08-25"}],
                                  completed_through=datetime(2026, 8, 25, 11, 22))
    result = engine.run("s01", context)
    assert result.total == 0
    assert result.exit_signal_hits[0]["signal_bar"] == 682
    context.positions[0]["available_qty"] = 0
    assert not engine.run("s01", context).exit_signal_hits


def _service(tmp_path, sell_volume=1000., missing=False):
    from types import SimpleNamespace

    from app.backtest.strategy import StrategyBacktestService

    entry, exit_day = date(2026, 8, 25), date(2026, 8, 26)
    previous = daily()
    later = previous.tail(1).with_columns(pl.lit(entry).alias("date"))
    final = later.with_columns(pl.lit(exit_day).alias("date"))
    panel = pl.concat([previous, later, final])
    partitions = {entry: bars(entry, end=891),
                  exit_day: bars(exit_day, end=890).with_columns(pl.lit(sell_volume).alias("volume"))}
    if missing:
        partitions[exit_day] = partitions[exit_day].filter(
            pl.col("datetime") != datetime(2026, 8, 26, 10, 0))

    class Repo:
        store = SimpleNamespace(data_dir=tmp_path)

        def get_instruments_asset(self, asset_type):
            return pl.DataFrame({"symbol": ["600001.SH"], "name": ["正常股票"],
                                 "listing_date": [date(2020, 1, 1)]})

        def list_minute_dates(self, start, end, asset_type):
            return list(partitions)

        def get_minute_by_dates(self, symbols, days, asset_type):
            return partitions[days[0]]

    class Engine:
        repo = Repo()

        def data_generation(self, asset_type):
            return None

        def load_panel_for_backtest(self, *args, **kwargs):
            return panel

    return StrategyBacktestService(Engine(), StrategyEngine([BUILTINS]))


@pytest.mark.parametrize("missing", [False, True])
def test_service_reconstructs_cash_and_rejects_missing_morning(tmp_path, missing):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    result = _service(tmp_path, missing=missing).run(
        StrategyBacktestConfig("s01", None, date(2026, 8, 25), date(2026, 8, 26),
                               overrides={"params": {"market_min_valid": 1}}),
        result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False),
    )
    assert result.config["initial_capital"] == 100000
    assert result.fills
    if missing:
        assert "105根" in result.error
        assert result.stats["status"] == "rejected_before_NAV"
        assert not result.equity_curve
        assert result.stats["execution"]["remaining_positions"][0]["remaining"] > 0
    else:
        assert result.error is None
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade["entry_value"] <= 20000
        assert result.stats["final_equity"] == pytest.approx(100000 + trade["pnl_amount"], abs=.005)
        assert result.stats["execution"]["remaining_positions"] == []
        assert all(676 <= fill["bar"] <= 689 for fill in result.fills if fill["side"] == "sell")
        assert result.signals[0]["signal_bar"] == 682


def test_service_deadline_keeps_actual_shares_and_cash(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    result = _service(tmp_path, sell_volume=1.).run(
        StrategyBacktestConfig("s01", None, date(2026, 8, 25), date(2026, 8, 26),
                               params={"market_min_valid": 1}),
        result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False),
    )
    assert "到期剩余" in result.error
    assert result.stats["execution"]["deadline_violations"] == 1
    pos = result.stats["execution"]["remaining_positions"][0]
    assert pos["remaining"] == pos["shares"]
    assert result.stats["execution"]["cash"] == pytest.approx(100000 - pos["entry_value"])
    assert not result.trades


def test_service_rejects_missing_first_trading_day_in_daily_axis(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    load = service.engine.load_panel_for_backtest
    service.engine.load_panel_for_backtest = lambda *args, **kwargs: load(*args, **kwargs).filter(
        pl.col("date") != date(2026, 8, 25))
    result = service.run(
        StrategyBacktestConfig("s01", None, date(2026, 8, 25), date(2026, 8, 26)),
        result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False),
    )
    assert result.error and "2026-08-25" in result.error
    assert not result.equity_curve


def test_api_detail_and_saved_execution_parameters(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import strategy as api

    engine = StrategyEngine([BUILTINS])
    monkeypatch.setattr(api, "_get_engine", lambda request: engine)
    monkeypatch.setattr(api, "_data_dir", lambda request: tmp_path)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        detail = client.get("/api/strategies/s01").json()
        assert detail["minute_execution"] == "overnight_capacity_v1"
        assert detail["params_defaults"]["entry_nav_fraction"] == .2
        assert detail["params_defaults"]["participation"] == .1
        saved = client.post("/api/strategies/config", json={
            "strategy_id": "s01", "overrides": {"params": {"participation": .05}}})
        assert saved.status_code == 200
        assert client.get("/api/strategies/s01").json()["params_defaults"]["participation"] == .05
        invalid = client.post("/api/strategies/config", json={
            "strategy_id": "s01", "overrides": {"params": {"guard_time": "11:30"}}})
        assert invalid.status_code == 400


def test_default_capacity_is_ten_percent():
    from app.backtest.overnight import fill_order

    source = bars(end=889).with_columns(pl.lit(40.).alias("volume"))
    result = fill_order(source, "buy", "600001.SH", 10., 887, 889, {}, budget=20000.)
    assert [f["shares"] for f in result["fills"]] == [400, 400, 400]


def test_dynamic_budget_scales_with_capital_and_compounds(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    policy = BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False)
    results = [service.run(
        StrategyBacktestConfig("s01", None, date(2026, 8, 25), date(2026, 8, 26),
                               params={"market_min_valid": 1, "initial_cash": cash}),
        result_policy=policy) for cash in (100000, 200000)]
    assert all(r.error is None for r in results)
    assert results[1].trades[0]["shares"] > results[0].trades[0]["shares"] * 1.9
    assert results[1].trades[0]["entry_value"] <= 40000
    assert results[1].trades[0]["entry_nav"] == 200000

    from app.strategy.builtin._overnight import entry_budget

    assert entry_budget(120000, 120000, {}) == 24000
    assert entry_budget(80000, 80000, {}) == 16000
    assert entry_budget(120000, 10000, {}) == 10000
    assert entry_budget(120000, 120000, {"entry_budget": 20000}) == 20000


@pytest.mark.parametrize("fraction", [0, 1.01, float("nan")])
def test_invalid_nav_fraction_rejected(fraction):
    from app.strategy.builtin._overnight import parameters

    with pytest.raises(ValueError):
        parameters({"entry_nav_fraction": fraction})


def test_batch_orders_use_known_nav_and_the_same_system_entry_window(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    load = service.engine.load_panel_for_backtest
    original = load()
    service.engine.load_panel_for_backtest = lambda *args, **kwargs: pl.concat([
        original, original.with_columns(pl.lit("600002.SH").alias("symbol"))])
    service.engine.repo.get_instruments_asset = lambda asset: pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"], "name": ["正常股票"] * 2,
        "listing_date": [date(2020, 1, 1)] * 2})
    get = service.engine.repo.get_minute_by_dates

    def minutes(symbols, days, asset):
        f = get(symbols, days, asset)
        if days[0] == date(2026, 8, 25):
            f = f.with_columns(pl.lit(40.).alias("volume"))
            # A later completed bar must not influence the earlier budget.
            f = f.with_columns(pl.when(pl.col("datetime").dt.minute() == 50)
                               .then(100.).otherwise(pl.col("close")).alias("close"))
        return pl.concat([f, f.with_columns(pl.lit("600002.SH").alias("symbol"))])

    service.engine.repo.get_minute_by_dates = minutes
    result = service.run(StrategyBacktestConfig(
        "s01", None, date(2026, 8, 25), date(2026, 8, 26), params={"market_min_valid": 1}),
        result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    buys = [f for f in result.fills if f["side"] == "buy"]
    assert [f["bar"] for f in buys] == [887, 887]
    trades = {t["symbol"]: t for t in result.trades}
    first = trades["600001.SH"]
    expected_nav = 100000 - first["entry_value"] + first["shares"] * 10.1
    assert trades["600002.SH"]["entry_nav"] == pytest.approx(expected_nav)
    assert trades["600002.SH"]["entry_value"] <= expected_nav * .2


def test_sell_signal_deadline_extends_to_1330_and_old_override_is_readable():
    from app.strategy.builtin._overnight import exit_signal

    assert exit_signal(pl.DataFrame(), 682, None, {})["last_eligible_bar"] == 810
    assert exit_signal(pl.DataFrame(), 682, None, {"sell_deadline": "11:29"})["last_eligible_bar"] == 689


@pytest.mark.parametrize("deadline", ["11:22", "12:30", "15:01"])
def test_invalid_sell_deadline_rejected(deadline):
    from app.strategy.builtin._overnight import parameters

    with pytest.raises(ValueError):
        parameters({"sell_deadline": deadline})


def test_service_sells_remaining_at_1330_without_lunch_fills(tmp_path):
    from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig

    service = _service(tmp_path)
    original = service.engine.repo.get_minute_by_dates

    def minutes(symbols, days, asset):
        f = original(symbols, days, asset)
        if days[0] == date(2026, 8, 26):
            bar = f["datetime"].dt.hour().cast(pl.Int32) * 60 + f["datetime"].dt.minute()
            f = f.with_columns(pl.when((bar >= 676) & (bar != 810))
                               .then(0.).otherwise(pl.col("volume")).alias("volume"))
        return f

    service.engine.repo.get_minute_by_dates = minutes
    policy = BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False)
    result = service.run(StrategyBacktestConfig(
        "s01", None, date(2026, 8, 25), date(2026, 8, 26),
        params={"market_min_valid": 1}), result_policy=policy)
    assert result.error is None
    assert [f["bar"] for f in result.fills if f["side"] == "sell"] == [810]
    assert result.stats["execution"]["remaining_positions"] == []
    old = service.run(StrategyBacktestConfig(
        "s01", None, date(2026, 8, 25), date(2026, 8, 26),
        params={"market_min_valid": 1, "sell_deadline": "11:29"}), result_policy=policy)
    assert old.error and "11:29" in old.error


def test_monitor_deadline_is_not_elapsed_before_1330():
    from app.strategy.overnight_runtime import exit_hits

    context = StrategyDataContext(
        "stock", "1m", date(2026, 8, 26), history=bars(date(2026, 8, 26), end=810),
        positions=[{"symbol": "600001.SH", "sell_date": "2026-08-26",
                    "qty": 100, "available_qty": 100, "entry_unit_cost": 10.1}],
        completed_through=datetime(2026, 8, 26, 13, 20))
    assert exit_hits(context, {})[0]["deadline_elapsed"] is False
    context.completed_through = datetime(2026, 8, 26, 13, 31)
    assert exit_hits(context, {})[0]["deadline_elapsed"] is True


def test_monitor_clock_guard_survives_missing_minute_partition(tmp_path, monkeypatch):
    from app.strategy import overnight_runtime as runtime
    from app.strategy.monitor import MonitorRuleEngine

    engine = StrategyEngine([BUILTINS])
    day = date(2026, 8, 25)
    monkeypatch.setattr(runtime, "cn_now", lambda: datetime(2026, 8, 25, 11, 22))
    monkeypatch.setattr(runtime, "position_snapshot", lambda *args: [
        {"symbol": "600001.SH", "qty": 100, "available_qty": 100,
         "entry_unit_cost": 10., "sell_date": str(day)}])

    class Service:
        def _load_enriched_history(self, day, bars):
            return daily()

        def build_strategy_context(self, *args, **kwargs):
            raise ValueError("无分钟K数据")

    context = runtime.monitor_context(Service()._load_enriched_history, engine,
                                      "s01", day, daily().tail(1), tmp_path)
    result = engine.run("s01", context)
    assert result.exit_signal_hits[0]["signals"] == ["overnight_time_guard"]
    assert MonitorRuleEngine()._signal_label("overnight_time_guard") == "隔夜时间清仓"
