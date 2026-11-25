"""Morning liquidation shares minute risk exits and completed T-1 regimes."""
from datetime import timedelta

import polars as pl
import pytest
from test_minute_backtest import _minute_frame
from test_morning_backtest import BUY, SELL, SYMBOL, configuration, morning_frame, service_fixture

from app.services import regime_builder


def run_risk(tmp_path, risk, frame=None, **options):
    if frame is None:
        frame = pl.concat([morning_frame(), _minute_frame(SELL, [
            (SYMBOL, f"10:{minute}", 10.4) for minute in range(31, 36)
        ])])
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]),
        SELL: frame,
    })
    config = configuration(initial_capital=1000000, max_exposure_pct=.01,
                           exit_fill="signal_next_minute", **options)
    config.overrides.update(risk)
    return service.run(config)


@pytest.mark.parametrize("risk,price,reason", [
    ({"stop_loss": -.03}, 9.8, "stop_loss"),
    ({"take_profit": .03}, 10.6, "take_profit"),
    ({"max_hold_days": 1}, 10.4, "max_hold"),
])
def test_risk_triggers_before_morning_deadline(tmp_path, risk, price, reason):
    result = run_risk(tmp_path, risk, morning_frame(price=price))
    assert not result.error, result.error
    trade, = result.trades
    assert trade["exit_reason"] == reason
    assert trade["exit_date"] == f"{SELL} 09:32"
    assert trade["exit_signal_date"] == f"{SELL} 09:31:00"
    assert result.stats["execution"]["remaining_positions"] == []
    assert sum(f["notional"] - f["fee"] if f["side"] == "sell" else
               -f["notional"] - f["fee"] for f in result.fills) + 1000000 == pytest.approx(
                   result.stats["execution"]["cash"])


@pytest.mark.parametrize("risk,reason", [
    ({"trailing_stop": .03}, "trailing_stop"),
    ({"trailing_take_profit_activate": .03, "trailing_take_profit_drawdown": .02},
     "trailing_take_profit"),
])
def test_trailing_risk_uses_prior_minute_peak_not_current_high(tmp_path, risk, reason):
    frame = morning_frame(price=10.4).with_columns(
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "09:31")
        .then(10.8).otherwise(pl.col("high")).alias("high"))
    result = run_risk(tmp_path, risk, frame)
    assert not result.error, result.error
    assert result.trades[0]["exit_reason"] == reason
    assert result.trades[0]["exit_signal_date"] == f"{SELL} 09:32:00"
    assert result.trades[0]["exit_date"] == f"{SELL} 09:33"


def test_regime_filters_new_entries_using_exact_previous_day(tmp_path, monkeypatch):
    seen = []
    def regimes(data_dir):
        seen.append(data_dir)
        return pl.DataFrame({"date": [BUY - timedelta(days=1), BUY],
                             "state": ["weak", "strong"], "score": [10., 90.]})
    monkeypatch.setattr(regime_builder, "load_regime_history", regimes)
    result = run_risk(tmp_path, {}, regime_filter={"states": ["strong"]})
    assert not result.error, result.error
    assert not [f for f in result.fills if f["side"] == "buy"]
    assert seen
    assert result.stats["execution"]["buy_regime_filter"] == 1


def test_missing_regime_evidence_fails_before_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(regime_builder, "load_regime_history", lambda _: pl.DataFrame())
    result = run_risk(tmp_path, {}, regime_filter={"min_score": 50})
    assert result.error and "环境数据" in result.error
    assert not result.equity_curve and not result.fills


def test_regime_rejection_does_not_block_existing_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(regime_builder, "load_regime_history", lambda _: pl.DataFrame({
        "date": [BUY - timedelta(days=1), BUY], "state": ["strong", "weak"], "score": [90., 10.]}))
    result = run_risk(tmp_path, {"stop_loss": -.03}, morning_frame(price=9.8),
                      regime_filter={"states": ["strong"]})
    assert not result.error, result.error
    assert len(result.trades) == 1
    assert result.trades[0]["exit_reason"] == "stop_loss"


def test_nontriggered_risk_preserves_morning_signal(tmp_path):
    result = run_risk(tmp_path, {"stop_loss": -.08, "take_profit": .2, "max_hold_days": 5})
    assert not result.error, result.error
    assert result.trades[0]["exit_reason"] == "signal"
    assert result.trades[0]["exit_date"] == f"{SELL} 10:31"


def test_risk_partial_sells_share_order_fees_and_respect_capacity(tmp_path):
    result = run_risk(tmp_path, {"stop_loss": -.03},
                      morning_frame(price=9.8).with_columns(pl.lit(1.).alias("volume")))
    assert not result.error, result.error
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert [f["shares"] for f in sells] == [100] * 4
    assert max(f["participation"] for f in sells) <= 1
    assert sum(f["fee"] for f in sells) == pytest.approx(result.trades[0]["exit_fee"])
    assert result.trades[0]["exit_date"] == f"{SELL} 09:35"


def test_risk_cannot_sell_on_entry_day_or_use_preentry_peak(tmp_path):
    service, _, _ = service_fixture(tmp_path, frames={
        BUY: _minute_frame(BUY, [(SYMBOL, "14:30", 10.2)]).with_columns(pl.lit(20.).alias("high")),
        SELL: morning_frame(),
    })
    config = configuration(initial_capital=1000000, max_exposure_pct=.01)
    config.overrides.update(stop_loss=-.03, trailing_stop=.03)
    result = service.run(config)
    assert not result.error, result.error
    assert result.trades[0]["exit_reason"] == "signal"
    assert all(f["date"] != str(BUY) for f in result.fills if f["side"] == "sell")


@pytest.mark.parametrize("broken", ["missing", "duplicate"])
def test_risk_does_not_certify_incomplete_signal_prefix(tmp_path, broken):
    frame = morning_frame(price=10.4).with_columns(
        pl.when(pl.col("datetime").dt.strftime("%H:%M") == "09:33")
        .then(9.8).otherwise(pl.col("low")).alias("low"))
    frame = frame.slice(1) if broken == "missing" else pl.concat([frame, frame.head(1)])
    result = run_risk(tmp_path, {"stop_loss": -.03}, frame)
    assert result.error and "前缀" in result.error
    assert not result.equity_curve


def test_risk_limit_down_failure_retains_actual_position(tmp_path):
    frame = morning_frame(price=9.).with_columns(
        *[pl.lit(9.).alias(c) for c in ("open", "high", "low", "close")])
    result = run_risk(tmp_path, {"stop_loss": -.03}, frame)
    assert result.error and "余股" in result.error
    assert result.stats["execution"]["remaining_positions"]
    assert not [f for f in result.fills if f["side"] == "sell"]
