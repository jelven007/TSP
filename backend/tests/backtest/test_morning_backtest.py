"""真实通用分钟入场与次交易日早盘清仓的连续资金回归。"""
from datetime import date, datetime, timedelta
from threading import Event

import polars as pl
import pytest
from test_minute_backtest import (
    TEST_STRATEGY_SOURCE,
    _daily_panel,
    _make_service,
    _minute_frame,
    _trading_days,
)

from app.backtest.strategy import StrategyBacktestConfig
from app.strategy import custom_signals

BUY = date(2026, 9, 4)
SELL = date(2026, 9, 7)
SYMBOL = "000001.SZ"
EXIT = "csgi_morning_liquidation"
OVERLAY = [{"left": "amount_mean_20d", "op": ">=", "right": "1.0",
            "leftDays": 0, "rightDays": 0}]


def morning_frame(day=SELL, *, price=10.4):
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=31)
    return _minute_frame(day, [
        (SYMBOL, (start + timedelta(minutes=i)).strftime("%H:%M"), price)
        for i in range(60)
    ])


def service_fixture(tmp_path, *, panel=None, frames=None):
    if panel is None:
        days = _trading_days(47, start=date(2026, 7, 6))
        panel = _daily_panel(days, [SYMBOL]).with_columns(
            pl.lit(10.0).alias("close"), pl.lit(10.0).alias("raw_close"),
            pl.lit(2e8).alias("amount"),
        )
    if frames is None:
        frames = {BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]),
                  SELL: morning_frame()}
    service = _make_service(tmp_path, panel, frames)
    source = TEST_STRATEGY_SOURCE.replace("* 1.05", "* 1.01").replace(
        'hits = joined.filter(', 'hits = joined.filter(pl.col("datetime").dt.hour() >= 14).filter(',
    )
    (tmp_path / "strategies/test_minute_ping.py").write_text(source)
    service.strategy_engine.reload()
    service.strategy_engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, {
        "id": "morning_liquidation", "name": "早盘清仓", "kind": "exit",
        "timeframe": "intraday", "template": "morning_liquidation",
        "conditions": [], "enabled": True,
        "params": {"time": "10:30:00", "amplitude_pct": 6.0},
    })
    return service, panel, frames


def configuration(**kwargs):
    values = {
        "strategy_id": "test_minute_ping", "symbols": [SYMBOL],
        "start": BUY, "end": SELL, "mode": "position",
        "initial_capital": 10000, "max_positions": 2,
        "commission_pct": .001, "minimum_commission": 5, "slippage_bps": 0,
        "overrides": {"exit_signals": [EXIT], "max_hold_days": None,
                      "stop_loss": None, "overlay_filter": OVERLAY,
                      "basic_filter": {"enabled": False}},
    }
    values.update(kwargs)
    return StrategyBacktestConfig(**values)


def test_friday_buy_monday_deadline_sell_with_daily_overlay(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    result = service.run(configuration())
    assert not result.error, result.error
    assert result.stats["n_trades"] == 1
    trade, = result.trades
    assert trade["entry_date"] == f"{BUY} 14:30"
    assert trade["exit_date"] == f"{SELL} 10:30"
    assert trade["entry_price"] == pytest.approx(10.2)
    assert trade["exit_price"] == pytest.approx(10.4)
    assert trade["shares"] == 400
    assert trade["entry_value"] == pytest.approx(4085)
    assert trade["exit_value"] == pytest.approx(4152.92)
    assert trade["pnl_amount"] == pytest.approx(67.92)
    assert result.stats["win_rate"] == 1
    assert result.stats["final_equity"] == pytest.approx(10067.92)
    assert [fill["fee"] for fill in result.fills] == pytest.approx([5, 7.08])
    assert [fill["participation"] for fill in result.fills] == pytest.approx([.004, .004])
    assert result.stats["execution"]["remaining_positions"] == []


def test_no_signal_is_valid_zero_trade_result(tmp_path):
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.0)]), SELL: morning_frame(),
    })
    result = service.run(configuration())
    assert not result.error, result.error
    assert result.stats["n_trades"] == 0
    assert result.stats["final_equity"] == 10000
    assert not result.fills


def run_frames(tmp_path, frame, **config):
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]), SELL: frame,
    })
    return service.run(configuration(**config))


def amplitude_frame():
    return morning_frame().with_columns(
        pl.when(pl.col("datetime").dt.time() == datetime.min.time().replace(hour=9, minute=40))
        .then(11.0).otherwise(pl.col("high")).alias("high"),
    )


@pytest.mark.parametrize("future_price", [10.4, 10.8])
def test_first_amplitude_trigger_ignores_later_prices(tmp_path, future_price):
    frame = amplitude_frame().with_columns(
        pl.when(pl.col("datetime").dt.time() > datetime.min.time().replace(hour=9, minute=40))
        .then(future_price).otherwise(pl.col("close")).alias("close"),
    )
    result = run_frames(tmp_path, frame)
    assert not result.error, result.error
    assert result.trades[0]["exit_date"] == f"{SELL} 09:40"
    assert result.trades[0]["exit_price"] == pytest.approx(10.4)


def test_partial_sells_use_one_minimum_commission_for_order(tmp_path):
    frame = amplitude_frame().with_columns(
        pl.lit(1.0).alias("volume"),
    )
    result = run_frames(tmp_path, frame)
    assert not result.error, result.error
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert len(sells) == 4 and [f["shares"] for f in sells] == [100] * 4
    assert sum(f["fee"] for f in sells) == pytest.approx(7.08)
    assert result.trades[0]["exit_date"] == f"{SELL} 09:43"
    assert result.trades[0]["pnl_amount"] == pytest.approx(67.92)
    assert sum(f["notional"] - f["fee"] if f["side"] == "sell"
               else -f["notional"] - f["fee"] for f in result.fills) + 10000 == pytest.approx(
                   result.stats["execution"]["cash"])


@pytest.mark.parametrize("kind", ["missing", "duplicate", "invalid", "seconds"])
def test_incomplete_prefix_cannot_certify_first_trigger(tmp_path, kind):
    frame = morning_frame()
    if kind == "missing":
        frame = frame.slice(1)
    elif kind == "duplicate":
        frame = pl.concat([frame, frame.head(1)])
    elif kind == "invalid":
        frame = frame.with_columns(
            pl.when(pl.col("datetime") == frame["datetime"][0]).then(None)
            .otherwise(pl.col("high")).alias("high"),
        )
    else:
        frame = frame.with_columns(
            pl.when(pl.col("datetime") == frame["datetime"][0])
            .then(pl.col("datetime") + timedelta(seconds=1)).otherwise(pl.col("datetime")),
        )
    result = run_frames(tmp_path, frame)
    assert result.error and "前缀" in result.error
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 400
    assert not result.trades and not result.equity_curve
    assert len(result.fills) == 1


@pytest.mark.parametrize("kind", ["zero_volume", "limit_down", "too_small"])
def test_deadline_failure_preserves_actual_fills_and_remaining(tmp_path, kind):
    frame = morning_frame()
    if kind == "zero_volume":
        frame = frame.with_columns(pl.lit(0.).alias("volume"))
    elif kind == "limit_down":
        frame = frame.with_columns(*[pl.lit(9.).alias(c) for c in ("open", "high", "low", "close")])
    else:
        frame = frame.with_columns(pl.lit(1.).alias("volume"))
    result = run_frames(tmp_path, frame)
    assert result.error and "余股" in result.error
    assert not result.trades and not result.equity_curve
    pos, = result.stats["execution"]["remaining_positions"]
    assert pos["remaining"] == (300 if kind == "too_small" else 400)
    assert len(result.fills) == (2 if kind == "too_small" else 1)
    assert result.stats["execution"]["cash"] == pytest.approx(6949.48 if kind == "too_small" else 5915)


def test_slippage_applied_once_on_each_leg(tmp_path):
    result = run_frames(tmp_path, morning_frame(), slippage_bps=10)
    assert not result.error, result.error
    trade = result.trades[0]
    assert trade["entry_price"] == pytest.approx(10.2102)
    assert trade["exit_price"] == pytest.approx(10.3896)
    assert trade["entry_value"] == pytest.approx(400 * 10.2102 + 5)
    assert trade["exit_value"] == pytest.approx(400 * 10.3896 * (1 - .0005) - 5)


def test_raw_cash_prices_independent_of_adjusted_daily_factor(tmp_path):
    service, panel, frames = service_fixture(tmp_path)
    adjusted = panel.with_columns(pl.col("close") * .5, pl.col("open") * .5,
                                  pl.col("high") * .5, pl.col("low") * .5)
    service, _, _ = service_fixture(tmp_path, panel=adjusted, frames=frames)
    result = service.run(configuration())
    assert not result.error, result.error
    assert result.trades[0]["entry_price"] == pytest.approx(10.2)
    assert result.trades[0]["exit_price"] == pytest.approx(10.4)
    assert result.stats["final_equity"] == pytest.approx(10067.92)


def test_partial_buy_and_last_day_open_position_valuation(tmp_path):
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]).with_columns(pl.lit(2.).alias("volume")),
    })
    result = service.run(configuration(end=BUY))
    assert not result.error, result.error
    assert not result.trades and result.stats["n_trades"] == 0
    assert result.fills[0]["shares"] == 200
    assert result.stats["unclosed_positions"] == 1
    assert result.stats["final_equity"] == pytest.approx(9955)
    assert result.stats["unrealized_pnl_amount"] == pytest.approx(-45)
    assert result.stats["max_drawdown"] == pytest.approx(-.0045)


def test_missing_sell_day_partition_does_not_roll_to_another_day(tmp_path):
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]),
    })
    result = service.run(configuration())
    assert result.error and "缺分钟" in result.error
    assert len(result.fills) == 1
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 400


def test_cancel_after_buy_retains_cash_and_position(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    event = Event()

    def progress(p):
        if p["date"] == str(SELL):
            event.set()

    result = service.run(configuration(), progress_cb=progress, cancel_event=event)
    assert result.error == "cancelled"
    assert len(result.fills) == 1 and not result.trades
    assert result.stats["execution"]["cash"] == pytest.approx(5915)


@pytest.mark.parametrize("patch, text", [
    ({"mode": "full"}, "连续资金"),
    ({"regime_filter": {"states": ["strong"]}}, "环境过滤"),
    ({"exit_fill": "signal_next_minute", "minute_fill": True}, "下一分钟"),
    ({"overrides": {"exit_signals": [EXIT, "signal_ma_death_5_20"]}}, "混合"),
    ({"overrides": {"exit_signals": [EXIT], "stop_loss": .05}}, "风控"),
])
def test_unsupported_options_are_explicitly_rejected(tmp_path, patch, text):
    service, _, _ = service_fixture(tmp_path)
    result = service.run(configuration(**patch))
    assert result.error and text in result.error
    assert not result.fills


def test_exact_previous_day_missing_is_explicit(tmp_path):
    _, panel, frames = service_fixture(tmp_path)
    service, _, _ = service_fixture(tmp_path, panel=panel.filter(pl.col("date") != date(2026, 9, 3)), frames=frames)
    result = service.run(configuration())
    assert result.error and "精确前交易日" in result.error
    assert not result.fills


def test_next_market_day_across_national_holiday(tmp_path):
    from app.market_time import published_trading_day

    buy, sell = date(2026, 9, 30), date(2026, 10, 8)
    day, dates = date(2026, 8, 1), []
    while day <= sell:
        if published_trading_day(day):
            dates.append(day)
        day += timedelta(days=1)
    panel = _daily_panel(dates, [SYMBOL]).with_columns(pl.lit(10.).alias("raw_close"),
                                                     pl.lit(10.).alias("close"), pl.lit(2e8).alias("amount"))
    service, _, _ = service_fixture(tmp_path, panel=panel, frames={
        buy: _minute_frame(buy, [(SYMBOL, "14:30", 10.2)]), sell: morning_frame(sell),
    })
    result = service.run(configuration(start=buy, end=sell))
    assert not result.error, result.error
    assert result.trades[0]["exit_date"] == f"{sell} 10:30"


def test_gap_after_partial_sale_retains_actual_sell_fragment(tmp_path):
    frame = amplitude_frame().with_columns(pl.lit(1.).alias("volume")).filter(
        pl.col("datetime").dt.time() != datetime.min.time().replace(hour=9, minute=41),
    )
    result = run_frames(tmp_path, frame)
    assert result.error and "成交窗口" in result.error
    assert len(result.fills) == 2
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300


def test_twenty_completed_daily_amounts_exclude_current_huge_amount(tmp_path):
    _, panel, frames = service_fixture(tmp_path)
    history = panel.filter(pl.col("date") < BUY).tail(20)
    panel = pl.concat([history, panel.filter(pl.col("date") >= BUY).with_columns(pl.lit(9e12).alias("amount"))])
    service, _, _ = service_fixture(tmp_path, panel=panel, frames=frames)
    result = service.run(configuration())
    assert not result.error and result.stats["n_trades"] == 1
    weak = panel.with_columns(
        pl.when(pl.col("date") < BUY).then(9e7).otherwise(pl.col("amount")).alias("amount"),
    )
    service, _, _ = service_fixture(tmp_path, panel=weak, frames=frames)
    result = service.run(configuration())
    assert not result.error and not result.fills


def test_nineteen_day_warmup_cannot_certify_overlay_result(tmp_path):
    _, panel, frames = service_fixture(tmp_path)
    panel = pl.concat([panel.filter(pl.col("date") < BUY).tail(19), panel.filter(pl.col("date") >= BUY)])
    service, _, _ = service_fixture(tmp_path, panel=panel, frames=frames)
    result = service.run(configuration())
    assert result.error and "预热" in result.error


@pytest.mark.parametrize("threshold, trades", [(60., 1), (60.01, 0)])
def test_real_late_session_signal_uses_full_pool_before_output_and_overlay(tmp_path, threshold, trades):
    symbols = ["000001.SZ", "000002.SZ", "600000.SH", "600001.SH", "600002.SH"]
    days = _trading_days(47, start=date(2026, 7, 6))
    panel = _daily_panel(days, symbols).with_columns(
        pl.lit(10.).alias("close"), pl.lit(10.).alias("raw_close"),
        pl.when(pl.col("symbol") == SYMBOL).then(2e8).otherwise(9e7).alias("amount"),
    )
    at = datetime(2026, 9, 4, 14, 30)
    frame = pl.concat([_minute_frame(BUY, [
        (symbol, (at - timedelta(minutes=59-i)).strftime("%H:%M"),
         10.1 + i * .001 if symbol in symbols[:3] else 9.9 - i * .001)
        for i in range(60)
    ]) for symbol in symbols])
    sell_frame = pl.concat([morning_frame(), _minute_frame(SELL, [
        (symbol, "14:30", 9.9) for symbol in symbols
    ])])
    service, _, _ = service_fixture(tmp_path, panel=panel, frames={BUY: frame, SELL: sell_frame})
    source = '''
import polars as pl
META = {"id": "test_minute_ping", "name": "s02", "asset_types": ["stock"],
        "timeframes": ["1m"], "result_sections": "signals", "scoring": {"change_pct": 1}}
EXECUTION_BACKEND = "minute_filter"
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
ENTRY_SIGNALS = ["csgi_late_session_strength"]
def filter_minute_history(df, params):
    return df.filter(pl.col("csgi_late_session_strength")).rename({"datetime": "last_datetime"})
'''
    (tmp_path / "strategies/test_minute_ping.py").write_text(source)
    service.strategy_engine.reload()
    custom_signals.save_one(tmp_path, {
        "id": "late_session_strength", "name": "尾盘走强", "kind": "entry",
        "timeframe": "intraday", "template": "late_session_strength",
        "conditions": [], "enabled": True,
        "params": {"time": "14:30:00", "change_min_pct": .4, "change_max_pct": 4.,
                   "pool_up_min_pct": threshold, "ma5_1m_count": 7, "ma5_5m_count": 4},
    })
    result = service.run(configuration())
    assert not result.error, result.error
    assert result.stats["n_trades"] == trades
    if trades:
        assert result.trades[0]["entry_price"] == pytest.approx(10.159)
        assert result.trades[0]["entry_date"] == f"{BUY} 14:30"
        assert result.trades[0]["entry_score"] == 50


def test_next_minute_open_cannot_spend_same_minute_close_sale_cash(tmp_path):
    symbols = [SYMBOL, "000002.SZ"]
    panel = _daily_panel(_trading_days(47, start=date(2026, 7, 6)), symbols).with_columns(
        pl.lit(10.).alias("close"), pl.lit(10.).alias("raw_close"), pl.lit(2e8).alias("amount"),
    )
    buys = _minute_frame(BUY, [(SYMBOL, "14:30", 10.2), (SYMBOL, "14:31", 10.2)])
    frame = pl.concat([amplitude_frame(), _minute_frame(SELL, [
        ("000002.SZ", "09:39", 10.2), ("000002.SZ", "09:40", 10.2),
    ])])
    service, _, _ = service_fixture(tmp_path, panel=panel, frames={BUY: buys, SELL: frame})
    path = tmp_path / "strategies/test_minute_ping.py"
    path.write_text(TEST_STRATEGY_SOURCE.replace("* 1.05", "* 1.01"))
    service.strategy_engine.reload()
    service.strategy_engine.get("test_minute_ping").meta["minute_entry_fill"] = "next_minute_open"
    result = service.run(configuration(symbols=symbols, max_positions=1))
    assert not result.error, result.error
    assert not [f for f in result.fills if f["symbol"] == "000002.SZ"]
    assert result.stats["execution"]["buy_no_slot"] >= 1
    assert result.trades[0]["entry_date"] == f"{BUY} 14:31"
    assert result.trades[0]["entry_signal_date"] == f"{BUY} 14:30"


@pytest.mark.parametrize("sizing, exposure, quantities", [
    ("equal", 1., [400, 400]), ("score_weight", 1., [400, 300]), ("equal", .5, [200, 200]),
])
def test_score_priority_and_portfolio_budgets(tmp_path, sizing, exposure, quantities):
    symbols = [SYMBOL, "000002.SZ", "600000.SH"]
    panel = _daily_panel(_trading_days(47, start=date(2026, 7, 6)), symbols).with_columns(
        pl.lit(10.).alias("raw_close"), pl.lit(10.).alias("close"), pl.lit(2e8).alias("amount"),
    )
    frame = _minute_frame(BUY, [(s, "14:30", p) for s, p in zip(symbols, [10.2, 10.3, 10.4], strict=True)])
    exits = pl.concat([morning_frame().with_columns(pl.lit(s).alias("symbol")) for s in symbols])
    service, _, _ = service_fixture(tmp_path, panel=panel, frames={BUY: frame, SELL: exits})
    service.strategy_engine.get("test_minute_ping").meta["scoring"] = {"change_pct": 1}
    result = service.run(configuration(symbols=symbols, position_sizing=sizing, max_exposure_pct=exposure))
    assert not result.error, result.error
    fills = [f for f in result.fills if f["side"] == "buy"]
    assert [f["symbol"] for f in fills] == ["600000.SH", "000002.SZ"]
    assert [f["shares"] for f in fills] == quantities
    assert result.stats["execution"]["cash"] >= 0
    assert result.stats["max_exposure"] <= exposure


def test_entry_data_failure_does_not_drop_existing_sale(tmp_path):
    service, _, _ = service_fixture(tmp_path)
    path = tmp_path / "strategies/test_minute_ping.py"
    source = path.read_text().replace(
        '# 因果性守卫:', 'if trigger_day.weekday() == 0:\n        raise ValueError("入场日线数据缺失")\n    # 因果性守卫:',
    )
    path.write_text(source)
    service.strategy_engine.reload()
    result = service.run(configuration())
    assert result.error and "入场日线数据缺失" in result.error
    assert len(result.trades) == 1 and len(result.fills) == 2
    assert result.stats["execution"]["remaining_positions"] == []
    assert result.stats["execution"]["cash"] == pytest.approx(10067.92)
    assert not result.equity_curve


def test_same_day_reentry_remains_blocked(tmp_path):
    frame = pl.concat([morning_frame(), _minute_frame(SELL, [(SYMBOL, "14:30", 10.2)])])
    result = run_frames(tmp_path, frame)
    assert not result.error, result.error
    assert result.stats["n_trades"] == 1
    assert result.stats["execution"]["buy_same_day_reentry"] == 1
    assert result.stats["execution"]["remaining_positions"] == []


def test_star_market_minimum_and_single_share_increment(tmp_path):
    symbol = "688001.SH"
    panel = _daily_panel(_trading_days(47, start=date(2026, 7, 6)), [symbol]).with_columns(
        pl.lit(10.).alias("close"), pl.lit(10.).alias("raw_close"), pl.lit(2e8).alias("amount"),
    )
    service, _, _ = service_fixture(tmp_path, panel=panel, frames={
        BUY: _minute_frame(BUY, [(symbol, "14:30", 10.2)]),
        SELL: morning_frame().with_columns(pl.lit(symbol).alias("symbol")),
    })
    result = service.run(configuration(symbols=[symbol], initial_capital=6000))
    assert not result.error, result.error
    assert result.fills[0]["shares"] == 293


def test_missing_known_pool_quote_is_failure_not_success_zero(tmp_path):
    symbols = [SYMBOL, "000002.SZ"]
    panel = _daily_panel(_trading_days(47, start=date(2026, 7, 6)), symbols).with_columns(
        pl.lit(10.).alias("close"), pl.lit(10.).alias("raw_close"), pl.lit(2e8).alias("amount"),
    )
    service, _, _ = service_fixture(tmp_path, panel=panel)
    custom_signals.save_one(tmp_path, {
        "id": "late_session_strength", "name": "尾盘走强", "kind": "entry",
        "timeframe": "intraday", "template": "late_session_strength",
        "conditions": [], "enabled": True,
    })
    overrides = {**configuration().overrides, "entry_signals": ["csgi_late_session_strength"]}
    result = service.run(configuration(overrides=overrides))
    assert result.error, "完整池缺少一个证券的触发分钟行情, 不可认证为零信号"


def test_unknown_volume_after_partial_sale_is_explicit_data_failure(tmp_path):
    frame = amplitude_frame().with_columns(
        pl.when(pl.col("datetime").dt.time() == datetime.min.time().replace(hour=9, minute=41))
        .then(None).otherwise(1.).alias("volume"),
    )
    result = run_frames(tmp_path, frame)
    assert result.error and "成交数据" in result.error
    assert len(result.fills) == 2 and not result.trades
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] == 300
