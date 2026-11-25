"""Exercise the installed S03 source through the real minute engine and A/B gates."""
from datetime import timedelta
from pathlib import Path

import polars as pl
import pytest
from test_entry_groups import AT, DAY, bars, signal, snapshot

from app.strategy import custom_signals
from app.strategy.engine import StrategyDataContext, StrategyEngine

SID = "custom_mvxxcs03"
SOURCE = Path(__file__).resolve().parents[2] / "examples/strategies/s03.py"


def test_current_source_keeps_ab_priority_and_frozen_scores(tmp_path):
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "s03.py").write_text(SOURCE.read_text())
    custom_signals.save_one(tmp_path, signal())
    engine = StrategyEngine([custom])
    previous = pl.DataFrame({"symbol": ["A", "AB", "B", "NO"],
                             "date": [DAY - timedelta(days=1)] * 4, "close": [10.] * 4})
    context = StrategyDataContext(
        "stock", "1m", DAY, history=bars(), daily_history=previous, pool_history=previous,
        current=previous.with_columns(pl.lit(DAY).alias("date")),
        observed_at=AT + timedelta(seconds=30), selection_snapshots={SID: snapshot()})
    result = engine.run(SID, context, overrides={
        "basic_filter": {"enabled": False, "target_mode": "all"},
        "entry_signals": ["csgi_late_session_strength_s03"], "exit_signals": [],
    })
    assert [row["symbol"] for row in result.rows] == ["A", "AB", "B"]
    assert [row["entry_group"] for row in result.rows] == ["A", "A", "B"]
    assert [row["score"] for row in result.rows] == [100., 50., 0.]
    assert all(row["selection_score"] == row["strategy_score"] == 80. for row in result.rows)
    assert all(str(row["last_datetime"]).replace(" ", "T") == AT.isoformat() for row in result.rows)
    assert all(not any(key.startswith("atr") for key in row) for row in result.rows)
    assert "csg_ma5_ma30" not in result.rows[0]
    assert result.entry_filter_error is None


@pytest.mark.parametrize("missing_funds", [False, True])
def test_current_source_uses_fund_order_with_opposite_atr_and_emotion(tmp_path, missing_funds):
    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "s03.py").write_text(SOURCE.read_text())
    custom_signals.save_one(tmp_path, signal())
    engine = StrategyEngine([custom])
    saved = snapshot()
    saved["rows"][1].update(atr14_previous_close=.01, emotion_score=None)
    saved["rows"].append({
        **saved["rows"][1], "symbol": "B_LOW", "selection_rank": 9,
        "fund_score": None if missing_funds else 50., "emotion_score": 100.,
        "atr14_previous_close": .99,
    })
    for row in saved["rows"]:
        row.setdefault("atr14_previous_close", .1)
        for key in ("tech_score", "fund_score", "emotion_score"):
            if row.get(key) is not None:
                row[key] = float(row[key])
    frame = pl.concat([bars(), bars().filter(pl.col("symbol") == "B").with_columns(
        pl.lit("B_LOW").alias("symbol"))])
    symbols = [row["symbol"] for row in saved["rows"]]
    previous = pl.DataFrame({"symbol": symbols, "date": [DAY - timedelta(days=1)] * len(symbols),
                             "close": [10.] * len(symbols)})
    context = StrategyDataContext(
        "stock", "1m", DAY, history=frame, daily_history=previous, pool_history=previous,
        current=previous.with_columns(pl.lit(DAY).alias("date")),
        observed_at=AT + timedelta(seconds=30), selection_snapshots={SID: saved})
    result = engine.run(SID, context, overrides={
        "basic_filter": {"enabled": False, "target_mode": "all"},
        "entry_signals": ["csgi_late_session_strength_s03"], "exit_signals": [],
    })
    expected = ["A", "AB", "B"] if missing_funds else ["A", "AB", "B", "B_LOW"]
    assert [row["symbol"] for row in result.rows] == expected
    assert [row["entry_priority"] for row in result.rows] == list(range(1, len(expected) + 1))
    assert all(row["strategy_score"] == row["selection_score"] == 80. for row in result.rows)
    assert all(str(row["last_datetime"]).replace(" ", "T") == AT.isoformat() for row in result.rows)
    assert all(not any(key.startswith("atr") for key in row)
               for kind in ("select", "buy") for row in result.sections[kind])
    assert saved["rows"][-1]["atr14_previous_close"] == .99
    assert result.entry_filter_error is None


def test_current_selection_drops_source_only_metrics_without_rewriting_frozen_input(tmp_path):
    from app.services.s03_selection import enrich_candidates

    path = tmp_path / "s03.py"
    path.write_text(SOURCE.read_text())
    engine = StrategyEngine([tmp_path])
    ranked = {"rows": [
        {"symbol": "A", "score": 80., "selection_rank": 1, "fund_score": 72.,
         "reference_close": 10., "atr_rank": 17, "atr14_previous_close": .05},
        {"symbol": "B", "score": 62., "selection_rank": 2, "fund_score": 95.,
         "reference_close": 11., "atr_rank": 1, "atr14_previous_close": .85},
    ]}
    context = StrategyDataContext("stock", "1m", DAY)
    actual = enrich_candidates(engine, context, ranked, {})
    assert actual["rows"] == [
        {"symbol": "A", "score": 80., "selection_rank": 1, "fund_score": 72., "reference_close": 10.},
        {"symbol": "B", "score": 62., "selection_rank": 2, "fund_score": 95., "reference_close": 11.},
    ]
    assert [row["atr_rank"] for row in ranked["rows"]] == [17, 1]
    assert engine.get(SID).enrich_selection_fn is None
