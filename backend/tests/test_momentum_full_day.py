from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.backtest.momentum import sell_position
from app.strategy.engine import StrategyDataContext
from app.strategy.momentum_day import run_day
from app.strategy.momentum_rules import parameters

DAY = date(2026, 8, 26)
FULL = {"guard_time": "15:00", "sell_deadline": "15:00", "slippage": 0.}


def frame(symbol, trigger=900):
    bars = [*range(571, 691), *range(781, 901)]
    return pl.DataFrame({
        "symbol": [symbol] * len(bars),
        "datetime": [datetime.combine(DAY, datetime.min.time()) + timedelta(minutes=b) for b in bars],
        "open": [10.] * len(bars), "high": [10.7 if b == trigger else 10.1 for b in bars],
        "low": [9.9] * len(bars), "close": [10.] * len(bars),
        "volume": [1000.] * len(bars), "amount": [1e6] * len(bars),
    })


def position():
    return {"symbol": "600001.SH", "entry_date": "2026-08-25", "sell_date": str(DAY),
            "shares": 1000, "remaining": 1000, "entry_notional": 10000., "entry_value": 10005.,
            "last_mark": 10., "entry_nav": 10005., "entry_budget": 10005.}


@pytest.mark.parametrize("trigger", [630, 690, 781, 872, 873, 885, 900])
def test_full_day_target_and_no_lunch_requirement(trigger):
    context = StrategyDataContext("stock", "1m", DAY, history=frame("600001.SH", trigger),
        daily_history=pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 8, 25)],
                                   "raw_close": [10.]}))
    result = run_day(context, FULL, pl.DataFrame(),
                     previous_state={"cash": 0., "positions": [position()]})
    assert result["error"] is None, result["error"]
    assert result["fills"][-1]["bar"] == trigger
    assert result["trades"][0]["exit_reason"] == "gross_target"
    assert not result["state"]["positions"]


@pytest.mark.parametrize("limit", [1, 5])
def test_afternoon_cash_and_position_slots_cannot_fund_earlier_buy(limit):
    context = StrategyDataContext("stock", "1m", DAY,
        history=pl.concat([frame("600001.SH", 885), frame("600002.SH")]),
        daily_history=pl.DataFrame({"symbol": ["600001.SH", "600002.SH"],
            "date": [date(2026, 8, 25)] * 2, "raw_close": [10.] * 2}))
    selected = pl.DataFrame({"symbol": ["600002.SH"], "close": [10.],
                             "return_1430": [.01], "prev_d_amount20": [2e8]})
    result = run_day(context, {**FULL, "position_limit": limit}, selected,
                     previous_state={"cash": 0., "positions": [position()]})
    assert result["error"] is None, result["error"]
    assert not result["sections"]["buy"]
    assert result["entry_decisions"][0]["reason"] != "已成交"
    assert len(result["fills"]) == len(result["trades"]) == 1


def test_same_day_split_exit_preserves_minimum_commission_and_stop():
    source = frame("600001.SH").filter(
        pl.col("datetime").is_in([datetime(2026, 8, 26, 14, 32), datetime(2026, 8, 26, 14, 33)])
    ).with_columns(pl.lit(10.).alias("volume"), pl.lit(9.).alias("low"))
    p = parameters(FULL)
    pos = position()
    whole = sell_position(source, pos["symbol"], 10., pos, p)
    first = sell_position(source.head(1), pos["symbol"], 10., pos, p)
    carried = {**pos, "remaining": first["remaining"], "sell_notional": first["notional"],
               "sell_fee_paid": first["fee"], "stop_pending": first["stop_pending"],
               "day_sell_notional": first.get("day_sell_notional", first["notional"]),
               "sell_fee_date": str(DAY)}
    second = sell_position(source.tail(1), pos["symbol"], 10., carried, p)
    assert first["fee"] + second["fee"] == pytest.approx(whole["fee"])
    assert first["value"] + second["value"] == pytest.approx(whole["value"])
    assert second["remaining"] == 0 and second["stop_pending"]


@pytest.mark.parametrize("stop_bar", [None, 840, 885])
def test_full_day_guard_or_afternoon_net_stop(stop_bar):
    source = frame("600001.SH", trigger=901)
    if stop_bar is not None:
        source = source.with_columns(pl.when(
            pl.col("datetime") == datetime.combine(DAY, datetime.min.time()) + timedelta(minutes=stop_bar))
            .then(9.).otherwise(pl.col("low")).alias("low"))
    context = StrategyDataContext("stock", "1m", DAY, history=source,
        daily_history=pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 8, 25)],
                                   "raw_close": [10.]}))
    result = run_day(context, FULL, pl.DataFrame(),
                     previous_state={"cash": 0., "positions": [position()]})
    assert result["error"] is None
    assert result["fills"][-1]["bar"] == (stop_bar or 900)
    assert result["trades"][0]["exit_reason"] == ("net_stop" if stop_bar else "time_guard")


def test_sale_before_buy_can_fund_replacement_without_t0_sale():
    context = StrategyDataContext("stock", "1m", DAY,
        history=pl.concat([frame("600001.SH", 872), frame("600002.SH", 885)]),
        daily_history=pl.DataFrame({"symbol": ["600001.SH", "600002.SH"],
            "date": [date(2026, 8, 25)] * 2, "raw_close": [10.] * 2}))
    selected = pl.DataFrame({"symbol": ["600002.SH"], "close": [10.],
                             "return_1430": [.01], "prev_d_amount20": [2e8]})
    result = run_day(context, {**FULL, "position_limit": 1}, selected,
                     previous_state={"cash": 0., "positions": [position()]})
    assert result["error"] is None
    assert [(f["side"], f["bar"]) for f in result["fills"]] == [("sell", 872), ("buy", 872)]
    assert len(result["trades"]) == 1
    assert len(result["state"]["positions"]) == 1
    assert result["state"]["positions"][0]["sell_date"] == "2026-08-27"


def test_new_sell_day_charges_new_minimum_commission():
    pos = {**position(), "day_sell_notional": 10000., "sell_fee_date": "2026-08-25"}
    result = sell_position(frame("600001.SH", 630), pos["symbol"], 10., pos, parameters(FULL))
    assert result["fee"] == pytest.approx(5. + result["notional"] * .0005)


@pytest.mark.parametrize("guard", ["12:00", "13:00", "15:01"])
def test_full_day_guard_rejects_nontrading_times(guard):
    with pytest.raises(ValueError):
        parameters({**FULL, "guard_time": guard})
