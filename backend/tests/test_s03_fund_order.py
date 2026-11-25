"""当前A/B资金优先序, 归档私有副本及缺失排序输入的回归。"""
from copy import deepcopy

import polars as pl
from test_entry_groups import AT, DAY, snapshot

from app.strategy.entry_groups import match
from app.strategy.selection_order import for_strategy

POLICY = "s03_ab_fund_v1"


def selection():
    saved = snapshot()
    base = saved["rows"][1]
    saved["rows"] = [
        {**saved["rows"][0], "atr14_previous_close": .01},
        {**base, "symbol": "B_LOW", "fund_score": 50., "emotion_score": 99.,
         "selection_rank": 6, "atr14_previous_close": .09},
        {**base, "symbol": "B_HIGH", "fund_score": 90., "emotion_score": None,
         "selection_rank": 9, "atr14_previous_close": .02},
        {**base, "symbol": "B_TIE", "fund_score": 90., "emotion_score": 1.,
         "selection_rank": 8, "atr14_previous_close": .03},
    ]
    return saved


def gate(saved):
    return match(
        {"a": {"enabled": True, "tech_min": 75},
         "b": {"enabled": True, "tech_min": 60}},
        saved, pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime}),
        day=DAY, at=AT, candidates={r["symbol"] for r in saved["rows"]}, previous={})


def test_current_source_replaces_ordering_marker_without_mutating_frozen_rows():
    saved = selection()
    saved["selection_ordering"] = "s03_selection_atr_desc_v1"
    original = deepcopy(saved)
    current = for_strategy(saved, {"selection_ordering": POLICY})
    assert current["selection_ordering"] == POLICY
    assert current["rows"] == original["rows"]
    assert saved == original


def test_priority_uses_funds_and_does_not_require_emotion():
    saved = {**selection(), "selection_ordering": POLICY}
    result, errors, coverage = gate(saved)
    assert list(result) == ["A", "B_TIE", "B_HIGH", "B_LOW"]
    assert [row["entry_priority"] for row in result.values()] == [1, 2, 3, 4]
    assert coverage == {"total": 4, "usable": 4, "passed": 4, "missing": 0}
    assert not errors


def test_missing_sorting_funds_is_isolated_even_when_threshold_is_disabled():
    saved = {**selection(), "selection_ordering": POLICY}
    saved["rows"][2]["fund_score"] = None
    result, errors, coverage = gate(saved)
    assert list(result) == ["A", "B_TIE", "B_LOW"]
    assert "fund_score" in errors["B_HIGH"]
    assert coverage["missing"] == 1
