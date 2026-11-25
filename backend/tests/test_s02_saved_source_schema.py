"""S02 saved selection rows must survive JSON dates and sparse inferred columns."""
import os
from datetime import date, datetime
from pathlib import Path

import polars as pl
import pytest

SOURCE = Path(os.environ.get(
    "TSP_S02_SCHEMA_SOURCE",
    str(Path(__file__).resolve().parents[2] / "examples/strategies/s02_atr14.py"),
))
PREVIOUS = date(2026, 5, 7)
TRIGGER = datetime(2026, 5, 8, 14, 30)


@pytest.fixture
def strategy():
    namespace = {}
    exec(compile(SOURCE.read_text(), str(SOURCE), "exec"), namespace)
    return namespace["filter_minute_history"]


def inputs():
    symbols = [f"600{i:03d}.SH" for i in range(101)]
    minute = pl.DataFrame({
        "symbol": symbols, "datetime": [TRIGGER] * 101,
        "close": [10.2] * 101, "csgi_late_session_strength": [True] * 101,
    })
    selection = {"source_date": str(PREVIOUS), "rows": [{
        "symbol": symbol, "reference_close": 10., "atr14": .2,
        "atr14_previous_close": .02, "atr_reference_date": None,
        "atr_rank": i + 1, "atr_rank_score": 100. - i, "atr_rank_population": 101,
    } for i, symbol in enumerate(symbols)]}
    return minute, selection


@pytest.mark.parametrize("date_form", ["fallback_string", "string_then_date", "date_then_string"])
def test_mixed_selection_dates_after_inference_window_preserve_hits_and_rank(strategy, date_form):
    minute, selection = inputs()
    if date_form == "string_then_date":
        for row in selection["rows"]:
            row["atr_reference_date"] = str(PREVIOUS)
    elif date_form == "date_then_string":
        for row in selection["rows"]:
            row["atr_reference_date"] = PREVIOUS
    selection["rows"][-1]["atr_reference_date"] = (
        str(PREVIOUS) if date_form == "date_then_string" else PREVIOUS
    )
    result = strategy(minute, {}, selection=selection)
    assert result["symbol"].to_list() == minute["symbol"].to_list()
    assert result["atr_reference_date"].dtype == pl.Date
    assert result["atr_reference_date"].to_list() == [PREVIOUS] * 101
    assert result["last_datetime"].to_list() == [TRIGGER] * 101
    assert result["atr_rank"].to_list() == list(range(1, 102))
    assert result["score"][0] == 100. and result["score"][-1] == 0.


def test_first_valid_atr_after_hundred_missing_rows_is_ranked_without_dropping_hits(strategy):
    minute, selection = inputs()
    for row in selection["rows"][:-1]:
        row["atr14"] = row["atr14_previous_close"] = None
        row["atr_rank"] = row["atr_rank_score"] = None
    selection["rows"][-1].update(atr_rank=1, atr_rank_score=50., atr_rank_population=1)
    result = strategy(minute, {}, selection=selection)
    assert result.height == 101
    assert result["symbol"][0] == minute["symbol"][-1]
    assert result["atr14_previous_close"].dtype == pl.Float64
    assert result["score"].to_list() == [50.] + [None] * 100
    assert result["atr_rank"].to_list() == [1] + [None] * 100


def test_first_atr_error_after_hundred_valid_rows_preserves_unknown_metrics(strategy):
    minute, selection = inputs()
    selection["rows"][-1].update(
        atr14=None, atr14_previous_close=None, atr_metric_error="缺少前日ATR",
    )
    result = strategy(minute, {}, selection=selection)
    assert result.height == 101
    assert result["atr_metric_error"].dtype == pl.String
    assert result["atr_metric_error"][-1] == "缺少前日ATR"
    assert result["atr_rank"][-1] is None
    assert result["score"][-1] is None


def test_all_missing_metrics_and_no_trigger_keep_existing_behavior(strategy):
    minute, selection = inputs()
    for row in selection["rows"]:
        row["atr14"] = row["atr14_previous_close"] = None
    result = strategy(minute, {}, selection=selection)
    assert result.height == 101
    assert result["score"].null_count() == 101
    assert strategy(
        minute.with_columns(pl.lit(False).alias("csgi_late_session_strength")),
        {}, selection=selection,
    ).is_empty()
