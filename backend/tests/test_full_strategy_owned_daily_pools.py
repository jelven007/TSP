"""Real tenant SQL -> task payload -> Compute and daily batch scope isolation."""
# ruff: noqa: F401,F811 -- reuse disposable PostgreSQL fixtures.
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_full_daily_stock_pools import env, full_cluster, owned, upgrade
from test_strategy_owned_daily_pools import DAY, SYMBOLS, identity, pool, source

from app.multitenancy.scheduled_worker import TenantSchedules
from app.multitenancy.strategies import TenantStrategyEngine, definition_payload
from app.services.daily_stock_pools import DailyStockPools
from app.services.strategy_results import execution_snapshot
from app.strategy.engine import StrategyEngine


def catalog(documents, tmp_path, sid):
    (tmp_path / f"{sid}.py").write_text(source(sid, "沪主板"))
    parsed = StrategyEngine([tmp_path])
    documents.put("strategy_sources", sid, {
        "source": "custom", "code": source(sid, "沪主板"),
        "definition": definition_payload(parsed.get(sid)),
    })


def test_tenant_tasks_freeze_full_owned_pool_for_exact_day(env, tmp_path):
    _, documents = owned(env)
    _, other = owned(env)
    sid = "custom_mvxxcs01"
    catalog(documents, tmp_path, sid)
    saved = DailyStockPools(documents).save(identity(sid), pool(sid, [
        f"60{i:04d}.SH" for i in range(701)]), started_at=10)
    DailyStockPools(other).save(identity(sid), pool(sid, ["600099.SH"]), started_at=10)
    engine = TenantStrategyEngine(documents, StrategyEngine([]), object())
    for current in (engine, execution_snapshot(engine, documents)):
        task = current.task("strategy", strategy_ids=[sid], as_of=str(DAY))
        assert task.get("daily_pool_snapshots", {}).get(sid) == saved
        assert current.task("strategy", strategy_ids=[sid],
                            as_of="2026-11-10").get("daily_pool_snapshots", {}) == {}


def test_full_completed_batch_builds_new_strategy_without_replacing_old_pools(env, tmp_path):
    _, documents = owned(env)
    calls = []

    def execute(task):
        calls.append(task)
        return {"pools": {sid: pool(sid, SYMBOLS[:2]) for sid in task["strategy_ids"]}, "errors": {}}

    worker = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    now = datetime(2026, 11, 11, 9, 0)
    try:
        worker.daily_pools(documents, {"frames": {}}, None, now, None)
        old = DailyStockPools(documents).read(calls[0]["strategy_ids"][0], DAY)
        catalog(documents, tmp_path, "custom_mvxxcs01")
        worker.daily_pools(documents, {"frames": {}}, None, now, None)
        assert calls[1]["strategy_ids"] == ["custom_mvxxcs01"]
        assert DailyStockPools(documents).read(old["strategy_id"], DAY)["id"] == old["id"]
        assert DailyStockPools(documents).read("custom_mvxxcs01", DAY) is not None
        worker.daily_pools(documents, {"frames": {}}, None, now, None)
        assert len(calls) == 2
    finally:
        worker.runtime.close()
