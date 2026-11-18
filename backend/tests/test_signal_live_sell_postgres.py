"""Live sell projection remains tenant-scoped with real PostgreSQL archives."""
from datetime import date

import pytest
from test_full_identity import env as env
from test_full_identity import full_cluster as full_cluster
from test_full_identity import register
from test_screener_history_api import runtime as runtime

from app import market_time
from app.api import screener
from app.multitenancy.documents import TenantDocuments
from app.services.strategy_results import StrategyResults, capture_identity


@pytest.mark.parametrize("prior_buys", [[], [{"symbol": "600001.SH", "close": 10.2}]])
def test_live_sell_projection_uses_owned_previous_buy_archive(env, runtime, monkeypatch, prior_buys):
    _, user = register(env)
    _, other_user = register(env)
    own = TenantDocuments(env["db"], env["identity"].context(user, None, "sell-own"))
    other = TenantDocuments(env["db"], env["identity"].context(other_user, None, "sell-other"))
    _, engine, request, _ = runtime
    engine.get("sample").meta.update(timeframes=["1m"], result_sections="signals")
    engine.get("sample").execution_backend = "minute_filter"
    monkeypatch.setattr(screener, "_private_data_dir", lambda _: own)
    monkeypatch.setattr(market_time, "cn_today", lambda: date(2026, 11, 18))
    monkeypatch.setattr("app.multitenancy.runtime.fingerprint", lambda *args, **kwargs: "runtime")
    identity = capture_identity(engine, own, "sample", timeframe="1m")
    for documents, rows in ((own, prior_buys), (other, [{"symbol": "OTHER"}])):
        StrategyResults(documents).save(identity, {
            "as_of": "2026-11-17", "rows": rows, "total": len(rows),
            "section_mode": "signals", "sections": {"sell": [], "select": [], "buy": rows},
            "section_statuses": {"buy": "success"},
        }, started_at=10)
    live = {
        "as_of": "2026-11-18", "asset_type": "stock", "timeframe": "1m",
        "runtime_identity": identity.runtime_identity, "total": 0, "rows": [],
        "section_mode": "signals", "sections": {"sell": [], "select": [], "buy": []},
        "section_statuses": {"sell": "waiting", "buy": "waiting"},
        "section_errors": {"sell": "卖出名单等待归档层补齐", "buy": "等待尾盘确认"},
    }
    monkeypatch.setattr(StrategyResults, "monitor_snapshot", lambda *args, **kwargs: {
        "fingerprint": "runtime", "results": {"sample": live},
    })
    result = screener.get_cached_result(
        "sample", request, as_of=date(2026, 11, 18), timeframe="1m")["result"]
    assert [row["symbol"] for row in result["sections"]["sell"]] == [row["symbol"] for row in prior_buys]
    assert "sell" not in result["section_errors"]
    assert result["section_errors"]["buy"] == "等待尾盘确认"
    assert result["source"] == "realtime"
