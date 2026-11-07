"""Only the authenticated Worker bridge can install private minute plans."""
# ruff: noqa: F811 -- fixture reuse.
from datetime import date
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_minute_signal_sections import scan  # noqa: F401

from app.cell.platform_market import router


def test_priority_bridge_requires_token_and_bounds_complete_pool(monkeypatch):
    app = FastAPI()
    app.include_router(router)
    calls = []
    app.state.minute_refresh = SimpleNamespace(set_priority=lambda owner, plan: calls.append((owner, plan)))
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "test-internal-only")
    monkeypatch.setattr("app.market_time.cn_today", lambda: date(2026, 11, 2))
    body = {"owner": "tenant", "day": "2026-11-02", "sequence": 2,
            "symbols": ["600001.SH", "600002.SH"], "selected": ["600002.SH"]}
    client = TestClient(app)
    path = "/internal/cell-market/minute-priority"
    assert client.post(path, json=body).status_code == 401
    headers = {"Authorization": "Bearer test-internal-only"}
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert calls[0][1]["symbols"] == body["symbols"]
    assert client.post(path, headers=headers, json={**body, "selected": ["600003.SH"]}).status_code == 422
    assert client.post(path, headers=headers, json={**body, "day": "2026-10-30"}).status_code == 422


def test_worker_plan_uses_full_scope_even_with_one_saved_selection(scan, monkeypatch, tmp_path):
    from app.services import daily_selections
    from app.services.minute_priority import build_plan
    engine, context, overrides = scan
    monkeypatch.setattr(daily_selections.DailySelections, "read",
                        lambda *args: {"rows": [{"symbol": "600001.SH"}]})
    service = SimpleNamespace(build_daily_pool_context=lambda *args, **kw: context)
    plan = build_plan(engine, service, tmp_path, ["custom_test"], context.as_of,
                      overrides={"custom_test": overrides}, current=context.current)
    assert len(plan["symbols"]) == 5
    assert plan["selected"] == ["600001.SH"]
