"""Daily entry signals must reach minute candidates from exact completed T-1."""
from datetime import date, datetime

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext, StrategyEngine


@pytest.mark.parametrize("has_previous", [True, False])
def test_projected_minute_candidates_receive_completed_daily_entry_signals(tmp_path, has_previous):
    directory = tmp_path / "strategies/custom"
    directory.mkdir(parents=True)
    (directory / "minute_daily.py").write_text(
        'import polars as pl\n'
        'META = {"id": "minute_daily", "name": "minute daily", "timeframes": ["1m"]}\n'
        'EXECUTION_BACKEND = "minute_filter"\n'
        'BASIC_FILTER = {"enabled": False}\n'
        'ENTRY_SIGNALS = ["csgi_late", "signal_volume_surge", "csg_ma5_ma30", "csg_ma30_up"]\n'
        'def filter_minute_history(df, params, *, daily=None):\n'
        '    return df.select("symbol", "close", pl.col("datetime").alias("last_datetime"))\n',
    )
    engine = StrategyEngine([directory])
    symbols = ["600001.SH", "600002.SH", "600003.SH"]
    day, prior = date(2026, 11, 11), date(2026, 11, 10)
    minute = pl.DataFrame({
        "symbol": symbols, "datetime": [datetime(2026, 11, 11, 14, 50)] * 3,
        "close": [10.] * 3, "csgi_late": [True] * 3,
    })
    previous = pl.DataFrame({
        "symbol": symbols, "date": [prior] * 3, "close": [9.] * 3,
        "signal_volume_surge": [True, True, False],
        "csg_ma5_ma30": [True, False, True], "csg_ma30_up": [True] * 3,
    })
    # T is deliberately inverted. It cannot decide a 14:50 entry.
    today = previous.with_columns(
        pl.lit(day).alias("date"),
        (~pl.col("signal_volume_surge")).alias("signal_volume_surge"),
        (~pl.col("csg_ma5_ma30")).alias("csg_ma5_ma30"),
    )
    daily = pl.concat([previous, today]) if has_previous else today
    result = engine.run("minute_daily", StrategyDataContext(
        "stock", "1m", day, current=today, history=minute, daily_history=daily,
    ))
    assert [row["symbol"] for row in result.rows] == (["600001.SH"] if has_previous else [])
    if has_previous:
        assert result.entry_signal_hits[0]["signals"] == [
            "csgi_late", "signal_volume_surge", "csg_ma5_ma30", "csg_ma30_up",
        ]
