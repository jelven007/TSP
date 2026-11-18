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
