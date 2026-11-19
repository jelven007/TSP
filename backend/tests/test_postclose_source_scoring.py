"""Nightly source-owned ranking must survive generic old scoring overrides."""
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext, StrategyEngine
from app.strategy.signal_sections import select_rows


def test_s01_postclose_uses_raw_saved_range_amount_weight_and_keeps_unknown(tmp_path):
    source = Path(__file__).parent / "fixtures" / "s01_available_minutes.py"
    (tmp_path / "s01.py").write_bytes(source.read_bytes())
    engine = StrategyEngine([tmp_path])
    strategy = engine.get("custom_mvxxcs01")
    source_day = date(2026, 10, 30)
    previous = pl.DataFrame({"symbol": ["A", "B", "C"], "date": [date(2026, 10, 29)] * 3,
                             "close": [10., 10., 10.]})
    current = pl.DataFrame({
        "symbol": ["A", "B", "C"], "date": [source_day] * 3, "close": [10., 10., 10.],
        "high": [11., 10.1, 10.], "low": [10., 10., 10.], "amount": [1e8, 1e8, None],
    })
    context = StrategyDataContext("stock", "1m", date(2026, 11, 2), current=current,
                                  daily_history=pl.concat([previous, current], how="diagonal_relaxed"))
    rows = select_rows(engine, strategy, context, overrides={
        "basic_filter": {"enabled": False}, "scoring": {"change_pct": 999}})
    assert [r["symbol"] for r in rows] == ["B", "A", "C"]
    assert rows[0]["score"] == pytest.approx(-.01 + .001 * __import__("math").log(1e8))
    assert rows[-1]["score"] is None


def test_s02_postclose_applies_saved_atr_rank_as_score_when_generic_score_absent(monkeypatch):
    from test_s02_atr14_strategy import DAY, SOURCE, frames, prior

    from app.strategy import market_data
    monkeypatch.setattr(market_data, "previous_session", prior)
    engine = StrategyEngine([SOURCE.parent])
    strategy = engine.get("custom_mvxxcp88")
    _, daily = frames()
    current = daily.filter(pl.col("date") == prior(DAY)).with_columns(
        pl.lit(.02).alias("change_pct"), pl.lit(1e9).alias("float_shares"))
    context = StrategyDataContext("stock", "1m", DAY, current=current, daily_history=daily)
    rows = select_rows(engine, strategy, context, overrides={"basic_filter": {"enabled": False},
                                                             "scoring": {"change_pct": 999}})
    assert [row["score"] for row in rows] == [100., 0.]
    assert [row["atr_rank"] for row in rows] == [1, 2]
