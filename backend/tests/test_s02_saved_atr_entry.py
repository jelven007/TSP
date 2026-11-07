"""Buy ranking consumes the saved previous-session ATR, never recalculates daily bars."""
import importlib.util
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest


@pytest.fixture
def source():
    spec = importlib.util.spec_from_file_location(
        "s02_saved_atr", Path(__file__).resolve().parents[2] / "examples/strategies/s02_atr14.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_saved_atr_ranks_hits_with_bad_daily_history_and_retains_missing_atr(source, monkeypatch):
    monkeypatch.setattr(source, "get_atr14_sma_reference",
                        lambda *args: (_ for _ in ()).throw(AssertionError("buy must not recompute ATR")))
    frame = pl.DataFrame({
        "symbol": ["a", "b", "c"], "datetime": [datetime(2026, 11, 4, 14, 30)] * 3,
        "close": [10.2, 10.1, 10.3], "csgi_late_session_strength": [True] * 3,
    })
    saved = {"source_date": "2026-11-03", "rows": [
        {"symbol": "a", "atr14": .2, "atr14_previous_close": .02, "reference_close": 10.},
        {"symbol": "b", "atr14": .4, "atr14_previous_close": .04, "reference_close": 10.},
        {"symbol": "c", "atr14": None, "atr14_previous_close": None, "reference_close": 10.},
    ]}
    result = source.filter_minute_history(frame, {}, daily=pl.DataFrame(), selection=saved)
    assert result["symbol"].to_list() == ["b", "a", "c"]
    assert result["score"].to_list() == [100., 0., None]
    assert result["atr_rank"].to_list() == [1, 2, None]
    assert result["atr14_previous_close"].to_list() == [.04, .02, None]
    assert result["atr_metric_error"][-1]
    assert result["change_pct"].to_list() == pytest.approx([.01, .02, .03])


def test_all_saved_atr_missing_retains_all_buy_hits_with_unknown_scores(source):
    frame = pl.DataFrame({
        "symbol": ["b", "a"], "datetime": [datetime(2026, 11, 4, 14, 30)] * 2,
        "close": [10.1, 10.2], "csgi_late_session_strength": [True, True],
    })
    result = source.filter_minute_history(
        frame, {}, selection={"source_date": "2026-11-03", "rows": [
            {"symbol": "a", "reference_close": 10.}, {"symbol": "b", "reference_close": 10.}]})
    assert result["symbol"].to_list() == ["a", "b"]
    assert result["score"].to_list() == [None, None]


def test_engine_passes_selection_and_keeps_unknown_scores_without_daily_warmup(tmp_path):
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    (tmp_path / "sample.py").write_text('''
import polars as pl
META={"id":"sample","name":"sample","asset_types":["stock"],"timeframes":["1m"],
      "result_sections":"signals","daily_history_bars":21,"scoring_mode":"strategy","scoring":{}}
EXECUTION_BACKEND="minute_filter"
BASIC_FILTER={"enabled":False,"target_mode":"all"}
def filter_minute_history(df, params, *, daily=None, selection=None):
    assert selection["source_date"]=="2026-11-03"
    return df.group_by("symbol").agg(pl.col("datetime").max().alias("last_datetime")).with_columns(
        pl.when(pl.col("symbol")=="b").then(None).otherwise(100.).alias("score"))
''')
    engine = StrategyEngine([tmp_path])
    context = StrategyDataContext("stock", "1m", date(2026, 11, 4),
        current=pl.DataFrame({"symbol": ["a", "b"], "date": [date(2026, 11, 4)] * 2}),
        history=pl.DataFrame({"symbol": ["a", "b"],
                             "datetime": [datetime(2026, 11, 4, 14, 30)] * 2}),
        pool_history=pl.DataFrame({"symbol": ["a", "b"], "date": [date(2026, 11, 3)] * 2,
                                   "raw_close": [10., 10.]}),
        selection_snapshots={"sample": {"as_of": "2026-11-04", "source_date": "2026-11-03",
                                       "rows": [{"symbol": "a"}, {"symbol": "b"}]}})
    result = engine.run("sample", context)
    assert [row["symbol"] for row in result.rows] == ["a", "b"]
    assert result.rows[-1]["score"] is None
    assert "b" not in result.scores
    assert engine.minute_daily_history_bars(["sample"]) == 21
    assert engine.minute_entry_daily_history_bars(["sample"]) == 0
