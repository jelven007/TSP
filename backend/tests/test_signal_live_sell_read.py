"""Live entry updates must retain the exact prior-session buy archive."""
from datetime import date
from types import SimpleNamespace

import pytest
import test_screener_history_api

from app import market_time
from app.api import screener
from app.services import strategy_results


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    return test_screener_history_api.runtime.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("prior_buys", [[], [{"symbol": "600001.SH", "close": 10.2}]])
def test_live_entry_snapshot_keeps_dated_sell_members_and_confirmation(runtime, monkeypatch, prior_buys):
    storage, engine, request, calls = runtime
    strategy = engine.get("sample")
    strategy.meta.update(timeframes=["1m"], result_sections="signals")
    strategy.execution_backend = "minute_filter"
    day = date(2026, 11, 18)
    monkeypatch.setattr(market_time, "cn_today", lambda: day)
    identity = strategy_results.capture_identity(engine, storage, "sample", timeframe="1m")
    store = strategy_results.StrategyResults(storage)
    store.save(identity, {
        "as_of": "2026-11-17", "rows": prior_buys, "total": len(prior_buys),
        "sections": {"sell": [], "select": [], "buy": prior_buys},
        "section_mode": "signals", "section_statuses": {"buy": "success"},
    }, started_at=10)
    store.save(identity, {
        "as_of": str(day), "rows": [], "total": 0,
        "sections": {"sell": [], "select": [], "buy": []},
        "section_mode": "signals",
    }, started_at=20)
    saved = screener.get_cached_result("sample", request, as_of=day, timeframe="1m")["result"]
    live = {
        "as_of": str(day), "asset_type": "stock", "timeframe": "1m",
        "runtime_identity": identity.runtime_identity,
        "rows": [], "total": 0,
        "sections": {"sell": [], "select": [], "buy": []}, "section_mode": "signals",
        "section_statuses": {"sell": "waiting", "buy": "waiting"},
        "section_errors": {"sell": "卖出名单等待归档层补齐", "buy": "等待尾盘确认"},
    }
    request.app.state.monitor_engine = SimpleNamespace(latest_strategy_results=lambda: {"sample": live})
    current = screener.get_cached_result("sample", request, as_of=day, timeframe="1m")["result"]
    assert current["sections"]["sell"] == saved["sections"]["sell"]
    assert "sell" not in current["section_errors"]
    assert current["section_errors"]["buy"] == "等待尾盘确认"
    assert current["source"] == "realtime"
    assert calls == []


@pytest.mark.parametrize("live_buy,review_identity,newer_archive,expected", [
    ("expired", "current", False, "success"),
    ("failed", "current", False, "success"),
    ("rejected", "current", False, "rejected"),
    ("expired", "different", False, "expired"),
    ("expired", "current", True, "success"),
])
def test_s03_same_day_saved_buy_review_survives_incomplete_live_overlay(
    runtime, monkeypatch, live_buy, review_identity, newer_archive, expected,
):
    from dataclasses import replace

    storage, engine, request, calls = runtime
    sid = "custom_mvxxcs03"
    strategy = engine._strategies.pop("sample")
    strategy.meta.update(id=sid, timeframes=["1m"], result_sections="signals")
    strategy.execution_backend = "minute_filter"
    engine._strategies[sid] = strategy
    day = date(2026, 11, 18)
    monkeypatch.setattr(market_time, "cn_today", lambda: day)
    identity = strategy_results.capture_identity(engine, storage, sid, timeframe="1m")
    review_id = replace(identity, daily_record=True, selection_policy="review",
                        runtime_identity=(identity.runtime_identity if review_identity == "current"
                                          else "another-definition"))
    store = strategy_results.StrategyResults(storage)
    prior_rows = [{"symbol": "600001.SH", "close": 10.2}]
    store.save(identity, {
        "as_of": "2026-11-17", "rows": prior_rows, "total": 1, "section_mode": "signals",
        "sections": {"sell": [], "select": [], "buy": prior_rows},
        "section_statuses": {"buy": "success"},
    }, started_at=10)
    saved = store.save(review_id, {
        "as_of": str(day), "rows": [], "total": 0, "section_mode": "signals",
        "runtime_identity": review_id.runtime_identity,
        "sections": {"sell": [], "select": [], "buy": []},
        "section_statuses": {"buy": "success"},
        "execution_status": {"state": "success"},
        "original_realtime": {"record_id": "original", "buy_status": "expired"},
        "repair_provenance": {"kind": "post_close_signal_review_v1", "as_of": str(day)},
    }, started_at=20)
    live = {
        "as_of": str(day), "asset_type": "stock", "timeframe": "1m",
        "runtime_identity": identity.runtime_identity,
        "rows": [], "total": 0, "section_mode": "signals",
        "sections": {"sell": [], "select": [{"symbol": "600002.SH"}], "buy": []},
        "section_statuses": {"sell": "rejected", "buy": live_buy},
        "section_errors": {"buy": "原实时回执未完成"} if live_buy != "rejected" else {},
        "monitor_tasks": {"buy": {"state": live_buy, "published": live_buy == "rejected"}},
    }
    if newer_archive:
        store.save(identity, live, started_at=30)
    request.app.state.monitor_engine = SimpleNamespace(latest_strategy_results=lambda: {sid: live})
    current = screener.get_cached_result(sid, request, as_of=day, timeframe="1m")["result"]
    assert current["section_statuses"]["buy"] == expected
    assert current["sections"]["select"] == [{"symbol": "600002.SH"}]
    assert current["sections"]["sell"][0]["symbol"] == "600001.SH"
    assert current["monitor_tasks"]["buy"]["state"] == live_buy
    if expected == "success":
        assert current["buy_review_record_id"] == saved["record_id"]
        assert current["original_realtime"]["buy_status"] == "expired"
        assert "buy" not in current["section_errors"]
    else:
        assert not current.get("repair_provenance")
    assert store.detail(sid, saved["record_id"]) == saved
    assert calls == []
