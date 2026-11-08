"""Current S02 templates must not restore the retired daily amount gate."""
import json
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.strategy.basic_filter import previous_session
from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.signal_sections import select_rows

EXAMPLES = Path(__file__).resolve().parents[2] / "examples/strategies"
SID = "s02_atr14_example"


@pytest.fixture(autouse=True)
def synthetic_calendar(monkeypatch):
    """Keep the synthetic fixture independent of installed market data."""
    previous_session.cache_clear()
    monkeypatch.setattr(
        "app.strategy.basic_filter.published_trading_day", lambda day: day.weekday() < 5,
    )
    yield
    previous_session.cache_clear()


def selection_case(*, amount=2e7):
    """All members meet the current price, turnover, cap and change filters."""
    day = date(2026, 11, 2)
    before = day
    days = []
    for _ in range(21):
        before = previous_session(before)
        days.append(before)
    daily = pl.DataFrame([
        {"symbol": symbol, "date": at, "high": 10.5, "low": 9.5,
         "close": 10., "raw_close": 10., "change_pct": .02,
         "turnover_rate": 2., "float_shares": 1e9, "amount": amount}
        for symbol in ["600001.SH", "600002.SH"] for at in days
    ])
    if amount is None:
        daily = daily.drop("amount")
    current = daily.filter(pl.col("date") == previous_session(day))
    engine = StrategyEngine([EXAMPLES])
    overrides = json.loads((EXAMPLES / "s02_atr14.overrides.json").read_text())
    context = StrategyDataContext("stock", "1m", day, current=current, daily_history=daily)
    return engine, overrides, context


@pytest.mark.parametrize("amount", [2e7, None, 2e8], ids=["below_old_gate", "missing_amount", "above_old_gate"])
def test_current_template_keeps_qualified_selection_and_buy_members(amount):
    engine, overrides, context = selection_case(amount=amount)
    strategy = engine.get(SID)
    selected = select_rows(engine, strategy, context, overrides=overrides)
    assert {row["symbol"] for row in selected} == {"600001.SH", "600002.SH"}
    context.selection_snapshots = {SID: {
        "as_of": str(context.as_of), "source_date": "2026-10-30", "rows": selected,
    }}
    context.history = pl.DataFrame({
        "symbol": ["600001.SH", "600002.SH"],
        "datetime": [datetime(2026, 11, 2, 14, 30)] * 2,
        "close": [10.2, 10.2], "csgi_late_session_strength": [True, True],
    })
    # Trigger calculation is independent; exercise the real engine eligibility
    # and source-owned buy path with already confirmed signal columns.
    engine._inject_intraday_signal_columns = lambda frame, **kw: frame
    result = engine.run(SID, context, overrides=overrides)
    assert {row["symbol"] for row in result.sections["buy"]} == {"600001.SH", "600002.SH"}
    assert result.daily_stock_pool["total"] == 2


def test_current_template_still_enforces_other_numeric_filters():
    engine, overrides, context = selection_case(amount=2e8)
    context.daily_history = context.daily_history.with_columns(
        pl.when(pl.col("symbol") == "600002.SH").then(12.)
        .otherwise(pl.col("turnover_rate")).alias("turnover_rate"))
    selected = select_rows(engine, engine.get(SID), context, overrides=overrides)
    assert [row["symbol"] for row in selected] == ["600001.SH"]


def test_explicit_historical_amount_overlay_still_filters():
    engine, overrides, context = selection_case()
    overrides["overlay_filter"] = [
        {"left": "amount_mean_20d", "op": ">=", "right": "1.0", "leftDays": 0, "rightDays": 0},
    ]
    assert select_rows(engine, engine.get(SID), context, overrides=overrides) == []
