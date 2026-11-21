from dataclasses import replace
from datetime import date

import pytest
from test_screener_history_api import runtime as runtime

from app.api import screener
from app.services.daily_selections import DailySelections
from app.services.strategy_results import StrategyResults, capture_identity


@pytest.mark.parametrize("rows", [[], [{"symbol": "600001.SH", "close": 10.}]])
def test_today_preselection_reads_later_confirmed_sell_roster_without_computation(runtime, monkeypatch, rows):
    storage, engine, request, calls = runtime
    target = date(2026, 11, 20)
    monkeypatch.setattr("app.market_time.cn_today", lambda: target)
    strategy = engine.get("sample")
    strategy.meta.update(timeframes=["1m"], result_sections="signals")
    strategy.execution_backend = "minute_filter"
    identity = capture_identity(engine, storage, "sample", timeframe="1m")
    DailySelections(storage).save(identity, {
        "as_of": str(target), "source_date": "2026-11-19", "rows": [],
        "scheduled_for": "2026-11-19T16:00:00+08:00",
        "generated_at": "2026-11-19T16:01:00+08:00"}, started_at=1)
    store = StrategyResults(storage)
    store.save(identity, {
        "as_of": "2026-11-19", "section_mode": "signals", "rows": rows, "total": len(rows),
        "sections": {"sell": [], "select": [], "buy": rows},
        "section_statuses": {"buy": "success"}}, started_at=2)
    # A late roster publication must repair reads of the existing eligibility-only record.
    view = screener.get_cached_result("sample", request, as_of=target, timeframe="1m")["result"]
    assert view["section_statuses"]["sell"] == "success"
    assert "sell" not in view["section_errors"]
    assert [row["symbol"] for row in view["sections"]["sell"]] == [row["symbol"] for row in rows]
    assert view["section_errors"]["buy"]
    assert calls == []
    assert store.detail("sample", replace(identity, daily_record=True).record_id(str(target)))["result"]["section_errors"]["sell"]
