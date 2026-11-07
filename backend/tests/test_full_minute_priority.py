"""The authorized Full worker installs a complete private scheduling cohort."""
# ruff: noqa: F401,F811 -- disposable PostgreSQL fixture reuse.
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster
from test_full_runtime import owned


def test_full_worker_prepares_complete_pool_before_saved_selection_subset(owned, tmp_path, monkeypatch):
    from types import SimpleNamespace

    import polars as pl
    from test_minute_signal_sections import CODE

    from app.multitenancy.strategies import definition_payload
    from app.services.daily_selections import DailySelections
    from app.services.screener import ScreenerService
    from app.services.strategy_results import ResultIdentity
    from app.strategy.basic_filter import previous_session
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    code = CODE.replace("csgi_trigger", "csgi_late_session_strength")
    (tmp_path / "priority.py").write_text(code)
    definition = definition_payload(StrategyEngine([tmp_path]).get("custom_test"))
    owned.documents.put("strategy_sources", "custom_test",
        {"id": "custom_test", "source": "custom", "code": code, "definition": definition})
    owned.documents.put("custom_signals", "late_session_strength",
        {"id": "late_session_strength", "enabled": True, "template": "late_session_strength"})
    day = owned.frame["date"][0]
    source = previous_session(day)
    DailySelections(owned.documents).save(
        ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m"),
        {"as_of": str(day), "source_date": str(source),
         "rows": [{"symbol": "600519.SH"}]}, started_at=1)
    daily = owned.frame.with_columns(pl.lit(source).alias("date"), pl.lit(.02).alias("change_pct"))
    context = StrategyDataContext("stock", "1m", day, current=owned.frame, daily_history=daily)
    monkeypatch.setattr(ScreenerService, "build_daily_pool_context", lambda *args, **kwargs: context)
    plans = []
    owned.worker.market = SimpleNamespace(minute_priority=lambda owner, plan: plans.append((owner, plan)))
    owned.worker.prepare(owned.documents.context.tenant_id, owned.snapshot())
    assert plans[0][0] == owned.documents.context.tenant_id
    assert plans[0][1]["symbols"] == ["600000.SH", "600519.SH"]
    assert plans[0][1]["selected"] == ["600519.SH"]
