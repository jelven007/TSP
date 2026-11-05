from dataclasses import replace
from datetime import date

import pytest
import test_screener_history_api

from app.api import screener
from app.services import strategy_results
from app.services.daily_selections import DailySelections


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    return test_screener_history_api.runtime.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("day,selected,bought", [
    ("2026-11-04", 281, 4),
    ("2026-11-05", 574, 40),
])
def test_saved_signal_day_survives_dependency_change_and_selection_fallback(
    runtime, monkeypatch, day, selected, bought,
):
    storage, engine, request, calls = runtime
    engine.get("sample").meta.update(
        timeframes=["1m"], result_sections="signals",
    )
    old_dependencies = {"signals": [{"evaluation_delay_minutes": 1.5}]}
    monkeypatch.setattr(strategy_results, "_dependencies", lambda _: old_dependencies)
    key = strategy_results.capture_identity(engine, storage, "sample", timeframe="1m")
    rows = [{"symbol": f"BUY{i}", "close": 10 + i, "last_datetime": day + " 14:31:30"}
            for i in range(bought)]
    sections = {"sell": [], "select": [{"symbol": f"SELECT{i}"} for i in range(selected)],
                "buy": rows}
    store = strategy_results.StrategyResults(storage)
    saved = store.save(key, {"as_of": day, "rows": rows, "total": bought,
                            "sections": sections, "section_mode": "signals"}, started_at=20)
    if day == "2026-11-05":
        # The frozen eligibility record has a different key from minute execution.
        DailySelections(storage).save(
            key, {"as_of": day, "source_date": "2026-11-04", "rows": sections["select"],
                  "generated_at": "2026-11-04T16:00:00+08:00"}, started_at=10,
        )
    monkeypatch.setattr(strategy_results, "_dependencies",
                        lambda _: {"signals": [{"evaluation_delay_minutes": 1}]})
    monkeypatch.setattr(engine, "run", lambda *a, **kw: pytest.fail("history must not calculate"))
    summary = screener.get_cached_summary(request, as_of=date.fromisoformat(day), timeframe="all")
    assert summary["results"]["sample"]["record_id"] == saved["record_id"]
    assert summary["results"]["sample"]["total"] == bought
    result = screener.get_cached_result("sample", request, as_of=date.fromisoformat(day),
                                       timeframe="1m")["result"]
    assert result["record_id"] == saved["record_id"]
    assert result["sections"] == sections
    assert result["rows"] == rows
    assert calls == []
    assert store.detail("sample", saved["record_id"]) == saved


def test_signal_history_prefers_latest_run_including_successful_empty(runtime, monkeypatch):
    storage, engine, request, _ = runtime
    engine.get("sample").meta.update(timeframes=["1m"], result_sections="signals")
    key = strategy_results.capture_identity(engine, storage, "sample", timeframe="1m")
    old = replace(key, strategy_version="old-version")
    store = strategy_results.StrategyResults(storage)
    for identity, start, rows in [(key, 10, [{"symbol": "OLD"}]), (old, 30, [])]:
        latest = store.save(identity, {"as_of": "2026-11-04", "rows": rows,
            "total": len(rows), "sections": {"sell": [], "select": [], "buy": rows},
            "section_mode": "signals"}, started_at=start)
    result = screener.get_cached_result("sample", request, as_of=date(2026, 11, 4),
                                       timeframe="1m")["result"]
    assert result["record_id"] == latest["record_id"]
    assert result["rows"] == []
    assert result["total"] == 0
    assert screener.get_cached_result("sample", request, as_of=date(2026, 11, 3),
                                     timeframe="1m")["result"] is None
