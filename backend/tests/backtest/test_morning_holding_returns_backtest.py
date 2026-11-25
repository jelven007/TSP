"""Cash replay connects the actual filled average to the shared holding thresholds."""
import polars as pl
import pytest
from test_morning_backtest import SELL
from test_morning_optional_backtest import run_optional
from test_morning_returns_backtest import extended_frame


@pytest.mark.parametrize("key,price,reason", [
    ("take_profit_pct", 10.4, "take_profit"), ("stop_loss_pct", 9.59, "stop_loss"),
])
def test_cash_ledger_uses_filled_average_and_next_minute_exit(tmp_path, key, price, reason):
    data = extended_frame().with_columns(*[
        pl.lit(price).alias(field) for field in ("open", "high", "low", "close")
    ])
    result = run_optional(tmp_path, {key: 1 if reason == "take_profit" else 6,
                                     "start_time": "10:00:00"}, data)
    assert not result.error, result.error
    assert result.trades[0]["entry_price"] == pytest.approx(10.21)
    assert result.trades[0]["exit_signal_date"] == f"{SELL}T10:00:00"
    assert result.trades[0]["exit_date"] == f"{SELL} 10:01"
    hit, = [hit for hit in result.signals if hit["side"] == "sell"]
    assert hit["reason"] == reason
