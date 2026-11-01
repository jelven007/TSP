"""S02 must use the real Parquet projection and dependency checks before replay."""
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.minute_replay import minute_replay_feature_plan
from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestConfig,
    StrategyBacktestService,
)
from app.market_time import published_trading_days
from app.strategy.builtin import s02
from app.strategy.engine import StrategyEngine

START = date(2026, 8, 25)
END = date(2026, 8, 26)
SYMBOL = "600000.SH"
LIMIT_SIGNALS = {"signal_limit_up", "signal_limit_down", "signal_broken_limit_up"}


def _minutes(day):
    bars = [*range(571, 691), *range(781, 901)]
    prices = [10.1 + max(bar - 831, 0) * .001 for bar in bars]
    return pl.DataFrame({
        "symbol": [SYMBOL] * len(bars),
        "datetime": [datetime.combine(day, datetime.min.time()) + timedelta(minutes=bar)
                     for bar in bars],
        "open": prices, "high": [p + .01 for p in prices],
        "low": [p - .01 for p in prices], "close": prices,
        "volume": [1000.] * len(bars), "amount": [1e6] * len(bars),
    })


@pytest.fixture
def replay(tmp_path):
    prior = sorted(d for d in published_trading_days(2026) if d < START)[-25:]
    days = [*prior, START, END]
    prices = [9.9] * 24 + [10., 10.17, 10.17]
    daily = pl.DataFrame({
        "symbol": [SYMBOL] * len(days), "date": days,
        # Different adjusted/raw scales ensure S02 keeps its raw-price inputs.
        "open": [p / 2 for p in prices], "high": [(p + .01) / 2 for p in prices],
        "low": [(p - .01) / 2 for p in prices], "close": [p / 2 for p in prices],
        "raw_close": prices, "raw_high": [p + .01 for p in prices],
        "raw_low": [p - .01 for p in prices],
        "volume": [2e7] * len(days), "amount": [2e8] * len(days),
        "turnover_rate": [2.] * len(days),
    })
    path = tmp_path / "kline_daily_enriched" / "part.parquet"
    path.parent.mkdir()
    daily.write_parquet(path)
    minutes = {day: _minutes(day) for day in (START, END)}
    instruments = pl.DataFrame({
        "symbol": [SYMBOL], "name": ["合成股票"], "listing_date": [date(2000, 1, 1)],
        "total_shares": [1e9], "float_shares": [1e9],
    })
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments_asset=lambda _: instruments,
        get_historical_shares=lambda: pl.DataFrame(),
        get_instrument_history=lambda: pl.DataFrame(),
        execute_all=lambda *args: [],
        list_minute_dates=lambda start, end, _: sorted(d for d in minutes if start <= d <= end),
        get_minute_by_dates=lambda symbols, dates, _: minutes[dates[0]].filter(
            pl.col("symbol").is_in(symbols)),
    )
    registry = StrategyEngine([Path(s02.__file__).parent], override_loader=lambda _: {})
    # Use the real StrategyBacktestService, load_panel_for_backtest and Parquet scan.
    service = StrategyBacktestService(BacktestEngine(repo), registry)
    config = StrategyBacktestConfig(strategy_id="s02", symbols=None, start=START, end=END)
    return SimpleNamespace(service=service, config=config, path=path, minutes=minutes,
                           instruments=instruments)


def _run(replay):
    return replay.service.run(replay.config, result_policy=BacktestResultPolicy(include_monte_carlo=False))


def test_s02_replays_from_raw_daily_parquet_without_derived_limit_signals(replay):
    assert not LIMIT_SIGNALS & set(pl.read_parquet(replay.path).columns)
    result = _run(replay)
    assert result.error is None, result.error
    assert result.strategy_info["id"] == "s02"
    assert result.stats["minute_execution"] == "momentum_capacity_v1"
    assert len(result.daily_results) == len(result.equity_curve) == 2
    assert len(result.trades) == 1
    first, last = result.daily_results
    assert not first["sections"]["sell"]
    assert len(first["sections"]["select"]) == len(first["sections"]["buy"]) == 1
    assert len(last["sections"]["sell"]) == 1
    assert not last["sections"]["buy"]  # The final day closes the existing portfolio.
    buy, sell = result.fills
    assert buy["side"] == "buy" and buy["bar"] == 872
    high = replay.minutes[START].filter(pl.col("datetime").dt.time() == datetime.min.time().replace(
        hour=14, minute=32))["high"].item()
    assert buy["price"] == pytest.approx(high)
    assert buy["shares"] % 100 == 0 and buy["participation"] <= .5
    assert buy["notional"] + buy["fee"] <= 100000 * .2
    assert buy["fee"] == pytest.approx(5.)
    assert sell["side"] == "sell" and sell["date"] == str(END) and sell["bar"] == 630
    assert result.trades[0]["duration"] == 1
    assert last["state"]["complete"] and not last["state"]["positions"]
    assert result.config["params"]["stop_loss_pct"] == .06
    assert result.config["params"]["take_profit_pct"] == .06


@pytest.mark.parametrize("column", ["name", "float_shares"])
def test_s02_still_rejects_missing_required_instrument_fields(replay, column):
    replay.service.engine.repo.get_instruments_asset = lambda _: replay.instruments.drop(column)
    result = _run(replay)
    assert result.error and "回测字段依赖未生成" in result.error and column in result.error
    assert not result.fills and not result.trades and not result.equity_curve


def test_s02_still_rejects_missing_minute_partition_after_panel_load(replay):
    replay.minutes.pop(START)
    result = _run(replay)
    assert result.error == f"{START}缺少分钟分区"
    assert not result.fills and not result.equity_curve


def test_s02_still_rejects_a_missing_trading_day_in_daily_panel(replay):
    pl.read_parquet(replay.path).filter(pl.col("date") != START).write_parquet(replay.path)
    result = _run(replay)
    assert result.error == f"{START}缺少交易日日线"
    assert not result.fills and not result.equity_curve


def test_s02_preserves_bought_position_when_sell_window_is_incomplete(replay):
    replay.minutes[END] = replay.minutes[END].filter(
        pl.col("datetime") != datetime(2026, 8, 26, 10, 0))
    result = _run(replay)
    assert result.error and "卖出窗口缺少1根分钟" in result.error
    assert len(result.fills) == 1 and result.fills[0]["side"] == "buy"
    assert not result.trades and not result.equity_curve
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == result.fills[0]["shares"]


def test_s02_preserves_position_and_fills_when_sell_capacity_is_zero(replay):
    replay.minutes[END] = replay.minutes[END].with_columns(pl.lit(0.).alias("volume"))
    result = _run(replay)
    assert result.error and "11:30到期剩余" in result.error
    assert result.stats["status"] == "rejected_before_NAV"
    assert len(result.fills) == 1 and not result.trades and not result.equity_curve
    assert result.stats["execution"]["remaining_positions"]
    assert result.stats["execution"]["partial_equity_curve"]


def test_shared_minute_plan_still_materializes_limit_signals(replay):
    panel = replay.service.engine.load_panel_for_backtest(
        None, START - timedelta(days=80), END, minute_replay_feature_plan(25))
    assert set(panel.columns) >= LIMIT_SIGNALS
    assert panel.height == 27
