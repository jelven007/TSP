"""S01 common form controls affect the real cash and minute execution ledger."""
from datetime import datetime

import polars as pl
import pytest
from test_s01_equal_selected import END, SECOND, START, service
from test_s01_strategy import bars

from app.backtest.strategy import BacktestResultPolicy, StrategyBacktestConfig
from app.strategy import custom_signals


def test_overnight_names_survive_positions_and_completed_trades(tmp_path):
    result = replay(tmp_path, exit_signal=True)
    assert result.error is None
    assert result.trades
    assert {trade["name"] for trade in result.trades} == {"正常股票"}
    positions = replay(tmp_path, exit_signal=False).stats["execution"]["remaining_positions"]
    assert positions
    assert {position["name"] for position in positions} == {"正常股票"}


def replay(tmp_path, *, count=2, retained=False, partial=False, scores=None,
           exit_signal=False, **settings):
    subject = service(tmp_path, count, retained=retained, partial=partial)
    subject.strategy_engine.get("s01").meta["params"] = []
    if scores is not None:
        original = subject.strategy_engine.run

        def ranked(*args, **kwargs):
            result = original(*args, **kwargs)
            for row, score in zip(result.rows, scores, strict=False):
                row["score"] = score
            return result

        subject.strategy_engine.run = ranked
    if exit_signal:
        original_read = subject.engine.repo.get_minute_by_dates

        def complete(requested, days, asset):
            frame = original_read(requested, days, asset)
            extra = [bars(days[0], end=900, symbol=symbol).filter(
                pl.col("datetime").dt.hour().cast(pl.Int32).mul(60) + pl.col("datetime").dt.minute() > 890
            ).with_columns(pl.lit(1e6).alias("volume"))
                for symbol in frame["symbol"].unique().to_list()]
            return pl.concat([frame, *extra]).sort(["symbol", "datetime"])

        subject.engine.repo.get_minute_by_dates = complete
    definition = {"id": "exit", "name": "已完成前日条件", "kind": "exit", "enabled": True,
                  "conditions": [{"left": "close", "op": ">", "right": "1"}]}
    values = dict(
        initial_capital=1000000, max_positions=count, max_exposure_pct=1,
        commission_pct=0, minimum_commission=0, transfer_fee_pct=0,
        stamp_tax_pct=0, slippage_bps=0, exit_fill="signal_next_minute")
    values.update(settings)
    with custom_signals.private_signal_registry([definition]):
        return subject.run(StrategyBacktestConfig(
            "s01", None, START, END, params={"prior_return_min": 0},
            overrides={"entry_signals": [], "exit_signals": ["csg_exit"] if exit_signal else []},
            **values), result_policy=BacktestResultPolicy(
                include_benchmark=False, include_monte_carlo=False))


@pytest.mark.parametrize("count", [4, 5])
def test_common_equal_weight_uses_requested_position_count(tmp_path, count):
    result = replay(tmp_path, count=count)
    assert result.error is None
    positions = result.stats["execution"]["remaining_positions"]
    assert len(positions) == count
    assert all(p["entry_budget"] == pytest.approx(1000000 / count) for p in positions)
    assert result.config["max_positions"] == count
    assert result.config["entry_allocation"] == "common_backtest_v1"


def test_new_overnight_rule_selects_each_minute_low_in_the_cash_engine(tmp_path):
    result = replay(tmp_path, count=1, exit_signal=True, minute_price_rule="high_low_v1")
    assert not result.error, result.error
    sales = [fill for fill in result.fills if fill["side"] == "sell"]
    assert sales
    assert all(fill["price"] == pytest.approx(10.09) for fill in sales)


def test_common_equal_weight_reserves_unused_slots(tmp_path):
    result = replay(tmp_path, count=2, max_positions=4, max_exposure_pct=.8)
    assert result.error is None
    assert all(p["entry_budget"] == 200000 for p in result.stats["execution"]["remaining_positions"])
    assert result.stats["execution"]["cash"] >= 600000


@pytest.mark.parametrize("scores, expected", [([3, 1], [250000, 125000]),
                                            ([0, -1], [250000, 250000])])
def test_common_score_weight_and_single_stock_cap_match_standard_backtest(tmp_path, scores, expected):
    result = replay(tmp_path, max_positions=4, scores=scores, position_sizing="score_weight")
    assert result.error is None
    budgets = [p["entry_budget"] for p in result.stats["execution"]["remaining_positions"]]
    assert budgets == expected


def test_common_zero_exposure_keeps_cash_and_creates_no_orders(tmp_path):
    result = replay(tmp_path, max_exposure_pct=0)
    assert result.error is None
    assert result.fills == []
    assert result.stats["execution"]["cash"] == 1000000


def test_common_retained_holdings_consume_exposure_and_slots(tmp_path):
    result = replay(tmp_path, count=5, max_positions=4, max_exposure_pct=.6,
                    retained=True, partial=True)
    assert result.error is None
    positions = result.stats["execution"]["remaining_positions"]
    assert len(positions) == 4
    assert sum(p["entry_value"] for p in positions) <= 600000
    assert all(p["entry_budget"] <= 150000 for p in positions)
    assert result.stats["execution"]["cash"] >= 400000


@pytest.mark.parametrize("mode, sale_day, bar", [
    ("signal_next_minute", SECOND, 571), ("close_t", SECOND, 900), ("open_t+1", END, 571),
])
def test_common_clearance_choice_changes_actual_fill_time_and_keeps_signal_date(tmp_path, mode, sale_day, bar):
    result = replay(tmp_path, count=1, exit_signal=True, exit_fill=mode)
    assert result.error is None
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert sells and sells[0]["date"] == str(sale_day) and sells[0]["bar"] == bar
    assert result.trades[0]["exit_signal_date"].startswith(str(SECOND))
    assert result.config["exit_fill"] == mode
    assert all(f["date"] > str(START) for f in sells)


def test_common_close_exit_without_last_minute_retains_position_and_rejects_nav(tmp_path, monkeypatch):
    import app.backtest.overnight as kernel
    original = kernel.fill_order

    def missing_close(frame, *args, **kwargs):
        return original(frame.filter(pl.col("bar") != 900), *args, **kwargs)

    monkeypatch.setattr(kernel, "fill_order", missing_close)
    result = replay(tmp_path, count=1, exit_signal=True, exit_fill="close_t")
    assert result.error and "剩余" in result.error
    assert "15:00到期" in result.error
    assert result.stats["execution"]["remaining_positions"]
    assert result.equity_curve == []


def test_common_result_reports_actual_entry_semantics(tmp_path):
    result = replay(tmp_path)
    assert result.error is None
    assert result.config["execution_entry_fill"] == "system_five_minute_high_capacity_v1"
    assert result.config["execution_exit_fill"] == "signal_next_minute"
    assert all(datetime.fromisoformat(f["datetime"]).minute >= 47 for f in result.fills)


def test_common_automatic_stamp_tax_uses_actual_sale_day_and_preserves_auto_setting(tmp_path):
    result = replay(tmp_path, count=1, exit_signal=True, stamp_tax_pct=None)
    assert result.error is None
    assert result.config["stamp_tax_pct"] is None
    assert result.config["stamp_tax_mode"] == "historical"
    for day in (SECOND, END):
        sells = [f for f in result.fills if f["side"] == "sell" and f["date"] == str(day)]
        assert sum(f["fee"] for f in sells) == pytest.approx(sum(f["notional"] for f in sells) * .0005)


def test_common_close_sale_does_not_fund_or_free_a_slot_before_the_close(tmp_path):
    result = replay(tmp_path, count=1, exit_signal=True, exit_fill="close_t")
    assert result.error is None
    buys = [f for f in result.fills if f["side"] == "buy"]
    assert all(f["date"] == str(START) for f in buys)


def test_common_signal_sale_is_bounded_to_five_minutes_and_retains_actual_residual(tmp_path, monkeypatch):
    import app.backtest.overnight as kernel
    original = kernel.fill_order

    def small_capacity(frame, *args, **kwargs):
        return original(frame.with_columns(pl.lit(20.).alias("volume")), *args, **kwargs)

    monkeypatch.setattr(kernel, "fill_order", small_capacity)
    result = replay(tmp_path, count=1, exit_signal=True)
    assert result.error and "剩余" in result.error
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert [f["bar"] for f in sells] == [571, 572, 573, 574, 575]
    assert sum(f["shares"] for f in sells) == 10000
    assert result.stats["execution"]["remaining_positions"][0]["remaining"] > 0


def test_common_partial_close_sale_keeps_actual_sale_proceeds_in_rejected_cash_ledger(tmp_path, monkeypatch):
    import app.backtest.overnight as kernel
    original = kernel.fill_order

    def small_capacity(frame, *args, **kwargs):
        return original(frame.with_columns(pl.lit(20.).alias("volume")), *args, **kwargs)

    monkeypatch.setattr(kernel, "fill_order", small_capacity)
    result = replay(tmp_path, count=1, exit_signal=True, exit_fill="close_t")
    assert result.error and result.stats["execution"]["remaining_positions"]
    buys = [f for f in result.fills if f["side"] == "buy"]
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert sells and all(f["bar"] == 900 for f in sells)
    assert result.stats["execution"]["cash"] == pytest.approx(
        1000000 - sum(f["notional"] + f["fee"] for f in buys)
        + sum(f["notional"] - f["fee"] for f in sells))


def test_common_next_open_executes_latched_s01_signal_without_requiring_another_morning(tmp_path):
    from test_s01_parameter_signals import EXIT

    subject = service(tmp_path, 1)
    subject.strategy_engine.get("s01").meta["params"] = []
    original_read = subject.engine.repo.get_minute_by_dates

    def opening_only(requested, days, asset):
        frame = original_read(requested, days, asset)
        return frame.head(1) if days[0] == END else frame

    subject.engine.repo.get_minute_by_dates = opening_only
    with custom_signals.private_signal_registry([EXIT]):
        result = subject.run(StrategyBacktestConfig(
            "s01", None, START, END, params={"prior_return_min": 0},
            overrides={"entry_signals": [], "exit_signals": ["csgi_" + EXIT["id"]]},
            initial_capital=1000000, max_positions=1, exit_fill="open_t+1",
            commission_pct=0, minimum_commission=0, transfer_fee_pct=0,
            stamp_tax_pct=0, slippage_bps=0),
            result_policy=BacktestResultPolicy(include_benchmark=False, include_monte_carlo=False))
    assert result.error is None
    sells = [f for f in result.fills if f["side"] == "sell"]
    assert sells and sells[0]["date"] == str(END) and sells[0]["bar"] == 571
    assert result.trades[0]["exit_signal_date"] == f"{SECOND} 11:26"
