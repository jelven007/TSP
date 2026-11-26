import gzip
import json
from datetime import date, datetime

import polars as pl
import pytest

from app.services.s05_signal_scores import minute_returns
from app.services.strategy_results import ResultIdentity, StrategyResults

SID = "custom_mvxxcs05"
T, NEXT = "2026-11-26", "2026-11-27"
IDENTITY = ResultIdentity(SID, "s05", "saved", "{}", "{}", "null", "stock", "1m")


def view(store, day):
    return json.loads(gzip.decompress(store.day_view(SID, day)[1]))["result"]


@pytest.mark.parametrize(("branch", "expected", "sort"), [
    (1, 2.5, "return_5m"), (2, 68.0, "fund_tech"), (3, 92.0, "four_dimension"), (4, None, None),
])
def test_archived_buy_uses_frozen_branch_and_sell_reuses_that_value(tmp_path, branch, expected, sort):
    store = StrategyResults(tmp_path)
    bought = {"symbol": "600001.SH", "score": 99, "s05_return_5m_pct": 2.5}
    frozen = {"as_of": T, "branch_code": branch,
              "rows": [{"symbol": bought["symbol"], "s02_fund_score": 80.,
                        "s02_tech_score": 40., "s02_score": 92.}]}
    saved = store.save(IDENTITY, {
        "as_of": T, "section_mode": "signals", "rows": [bought], "total": 1,
        "sections": {"buy": [bought], "sell": [], "select": []},
        "section_statuses": {"buy": "success"}, "selection_snapshot": frozen,
    }, started_at=1)
    assert store.detail(SID, saved["record_id"])["result"]["rows"][0]["score"] == 99
    buy, = view(store, T)["sections"]["buy"]
    sell, = view(store, NEXT)["sections"]["sell"]
    assert (buy["signal_score"], buy["signal_score_rule"]) == (expected, sort)
    assert (sell["signal_score"], sell["signal_score_rule"], sell["entry_date"]) == (expected, sort, T)


def test_later_selection_cannot_turn_missing_minute_into_unrelated_score(tmp_path):
    store = StrategyResults(tmp_path)
    bought = {"symbol": "600001.SH", "score": 99}
    snap = {"as_of": T, "source_date": "2026-11-25", "rows": [
        {"symbol": bought["symbol"], "s02_score": 90.}]}
    store.save(IDENTITY, {
        "as_of": T, "section_mode": "signals", "rows": [bought], "total": 1,
        "sections": {"buy": [bought], "sell": [], "select": []},
        "section_statuses": {"buy": "success"},
        "selection_snapshot": {**snap, "branch_code": 1},
    }, started_at=1)
    buy, = view(store, T)["sections"]["buy"]
    assert buy["signal_score"] is None
    assert buy["signal_score_rule"] == "return_5m"


def test_five_minute_score_uses_exact_closed_bars_and_deduplicates_by_last_row():
    day = date.fromisoformat(T)
    rows = [{"symbol": symbol, "last_datetime": f"{T}T14:05:00"} for symbol in ("a", "b")]
    history = pl.DataFrame({
        "symbol": ["a", "a", "a", "a", "a", "b", "b"],
        "datetime": [datetime.fromisoformat(f"{T}T{s}:00") for s in (
            "14:00", "14:00", "14:05", "14:05", "14:06", "14:01", "14:05")],
        "close": [9., 10., 10.4, 10.2, 12., 10., 11.],
    })
    assert minute_returns(rows, history, day) == {f"a:{T}T14:05:00": pytest.approx(2)}
