"""The actual Full task refreshes custom pools with monitoring disabled."""
# ruff: noqa: F401,F811 -- real isolated PostgreSQL fixture reuse.
from datetime import datetime
from types import SimpleNamespace

import pytest
from test_custom_status_refresh import DAY, identity, status
from test_full_daily_selections import source
from test_full_daily_stock_pools import upgrade
from test_full_identity import env, full_cluster
from test_full_schedules import claim, own

from app.cell.context import AccessError
from app.cell.scheduler import Scheduler
from app.market_time import CN_TZ
from app.multitenancy.scheduled_worker import TenantSchedules
from app.services.custom_status_refresh import save_refresh
from app.services.daily_selections import DailySelections
from app.services.daily_stock_pools import DailyStockPools
from app.services.strategy_monitor_tasks import (
    monitored_ids,
    monitoring_summary,
    set_monitoring,
    task_receipt,
)
from app.services.strategy_results import StrategyResults
from app.strategy.engine import StrategyEngine


def test_full_task_refreshes_disabled_custom_pool_and_prunes_next_day_atomically(env, tmp_path, monkeypatch):
    from app.services import announced_stock_status
    _, documents = own(env)
    _, other = own(env)
    source(documents, tmp_path)
    set_monitoring(documents, "custom_test", "Test", False)
    DailySelections(documents).save(identity(), {
        "as_of": str(DAY), "source_date": "2026-10-30",
        "rows": [{"symbol": "600000.SH"}, {"symbol": "600001.SH"}]}, started_at=1)
    task_receipt(documents, "custom_test", "2026-10-30", {"tasks": {"select": {"count": 2}}})
    monkeypatch.setattr(announced_stock_status, "confirmed_status", lambda *_, **__: status())
    calls = []
    pool = {"strategy_id": "custom_test", "as_of": str(DAY),
            "symbols": ["600000.SH"], "total": 1}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(
        execute=lambda task: calls.append(task) or {"pools": {"custom_test": pool}, "errors": {}}))
    lease = claim(env, documents)
    now = datetime(2026, 10, 30, 21, tzinfo=CN_TZ)
    try:
        service.evening_status(documents, {}, object(), now, lease)
        service.evening_status(documents, {}, object(), now, lease)
        assert len(calls) == 1
        assert calls[0]["kind"] == "daily_stock_pools"
        assert calls[0]["strategy_ids"] == ["custom_test"]
        assert calls[0]["confirmed_status"]["suspended"] == ["600001.SH"]
        assert "custom_test" not in monitored_ids(documents)
        assert DailyStockPools(documents).read("custom_test", DAY)["symbols"] == ["600000.SH"]
        assert DailySelections(documents).read("custom_test", DAY)["rows"] == [{"symbol": "600000.SH"}]
        summary = monitoring_summary(StrategyEngine([tmp_path]), documents, now=now)
        selected_task = next(row for row in summary["strategies"] if row["strategy_id"] == "custom_test")
        assert selected_task["tasks"]["select"]["count"] == 1
        assert DailyStockPools(other).read("custom_test", DAY) is None
        with pytest.raises(ValueError), documents.transaction() as unit:
            save_refresh(unit, identity(), {**pool, "symbols": [], "total": 0},
                         status(), started_at=8 * 10**18)
            raise ValueError("rollback")
        assert DailyStockPools(documents).read("custom_test", DAY)["total"] == 1
        with StrategyResults(documents).deleting_strategy("custom_test"):
            pass
        with pytest.raises(AccessError):
            save_refresh(documents, identity(), pool, status(), started_at=8 * 10**18)
    finally:
        Scheduler(env["db"]).finish(lease, None)
        service.runtime.close()
