"""Real SQL isolation, atomic rollback and daily scheduling acceptance."""
# ruff: noqa: F401,F811 -- isolated PostgreSQL fixture reuse.
import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_daily_stock_pools import DAY, PRIOR, key, payload
from test_full_identity import env, full_cluster, register

from app.cell.admin import migrate
from app.cell.context import AccessError
from app.cell.scheduler import Scheduler
from app.multitenancy.documents import TenantDocuments
from app.multitenancy.scheduled_worker import TenantSchedules
from app.services.daily_stock_pools import DailyStockPools
from app.services.strategy_results import StrategyResults


@pytest.fixture(scope="module", autouse=True)
def upgrade(full_cluster):
    for kind, role in zip(("control", "data"), full_cluster["roles"], strict=True):
        migrate(full_cluster[kind], kind, role, "full", target_version=7)
    full_cluster["db"].settings = full_cluster["db"].settings.model_copy(update={"schema_version": 7})
    full_cluster["db"].check()


def owned(env):
    _, user = register(env)
    return user, TenantDocuments(env["db"], env["identity"].context(user, None, "pools"))


def test_sql_isolation_rollback_empty_success_and_delete_fence(env):
    _, documents = owned(env)
    _, other = owned(env)
    DailyStockPools(documents).save(key(), payload(["600000.SH"]), started_at=10)
    assert DailyStockPools(other).read("sample", DAY) is None
    with pytest.raises(ValueError), documents.transaction() as unit:
        DailyStockPools(unit).save(key(), payload([]), started_at=20)
        raise ValueError("force rollback")
    assert DailyStockPools(documents).read("sample", DAY)["total"] == 1
    with StrategyResults(documents).deleting_strategy("sample"):
        pass
    assert DailyStockPools(documents).read("sample", DAY) is None
    with pytest.raises(AccessError):
        DailyStockPools(documents).save(key(), payload([]), started_at=30)


def test_new_tenant_daily_schedule_is_awake_and_keeps_running_without_ai_tasks(env):
    _, documents = owned(env)
    assert env["data"].execute(
        "SELECT due_at FROM scheduler.work WHERE tenant_id=%s AND kind='scheduled'",
        (documents.context.tenant_id,),
    ).fetchone()["due_at"] is not None


def test_group_membership_change_wakes_daily_pools_without_document_fields(env):
    from app.multitenancy.watchlist import Watchlist
    _, documents = owned(env)
    watchlist = Watchlist(env["db"], documents.context)
    _, group = watchlist.create_group("pool source", symbols=["600000.SH"])
    assert group["id"]
    assert env["data"].execute(
        "SELECT due_at FROM scheduler.work WHERE tenant_id=%s AND kind='scheduled'",
        (documents.context.tenant_id,),
    ).fetchone()["due_at"] is not None


def test_sql_old_and_new_pool_definitions_remain_separate_and_legacy_readable(env):
    _, documents = owned(env)
    store = DailyStockPools(documents)
    old = store.save(key(), payload(["600000.SH"], as_of=str(PRIOR)), started_at=10)
    new = store.save(key(), payload(["600000.SH"], definition_version="scope_float_cap_v2"),
                     started_at=20)
    assert old["id"] != new["id"]
    assert "definition_version" not in store.read("sample", PRIOR, record_id=old["id"])
    assert store.read("sample", DAY)["id"] == new["id"]


def test_old_definition_completed_marker_regenerates_once_under_new_definition(env):
    from pathlib import Path

    from test_daily_stock_pools import frames

    from app.multitenancy.market import encode_frame
    from app.multitenancy.research import signature
    from app.multitenancy.runtime import fingerprint
    from app.strategy.engine import StrategyEngine

    _, documents = owned(env)
    builtins = StrategyEngine([Path(__file__).resolve().parents[1] / "app" / "strategy" / "builtin"])
    ids = [sid for sid, strategy in builtins._strategies.items()
           if not strategy.meta.get("research_only")
           and "stock" in strategy.meta.get("asset_types", ["stock"])]
    with documents.transaction() as unit:
        observed = fingerprint(unit.connection, include_state=False)
        old_marker = signature({"input": observed, "release": None, "day": str(DAY)})
        unit.put("daily_stock_pool_jobs", str(DAY), {"signature": old_marker, "completed": ids})
    calls = []

    def execute(task):
        calls.append(task)
        return {"pools": {sid: payload(["600000.SH"], strategy_id=sid,
                                      definition_version="scope_float_cap_v2")
                          for sid in task["strategy_ids"]}, "errors": {}}

    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    snapshot = {"days": {"stock": str(DAY)}, "frames": {"stock": encode_frame(frames())}}
    now = datetime(2026, 10, 26, 9, 40, tzinfo=ZoneInfo("Asia/Shanghai"))
    try:
        service.daily_pools(documents, snapshot, None, now, None)
        assert len(calls) == 1
        assert set(calls[0]["strategy_ids"]) == set(ids)
        marker = documents.get("daily_stock_pool_jobs", f"{DAY}:09:00")
        assert marker["signature"] != old_marker
        assert set(marker["completed"]) == set(ids)
        service.daily_pools(documents, snapshot, None, now, None)
        assert len(calls) == 1
        assert DailyStockPools(documents).read(ids[0], DAY)["definition_version"] == "scope_float_cap_v2"
    finally:
        service.runtime.close()


def test_two_premarket_sql_batches_need_no_today_quote_and_read_latest_config(env):
    from test_daily_pool_schedule import wall
    _, documents = owned(env)
    calls = []
    def execute(task):
        calls.append(task)
        return {"pools": {sid: payload(["600000.SH"], strategy_id=sid)
                          for sid in task["strategy_ids"]}, "errors": {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    snapshot = {"days": {"stock": "2026-10-23"}, "frames": {}}
    try:
        service.daily_pools(documents, snapshot, None, wall(7, 59), None)
        assert not calls
        service.daily_pools(documents, snapshot, None, wall(8), None)
        assert len(calls) == 1
        assert calls[0]["premarket"] is True
        sid = calls[0]["strategy_ids"][0]
        pool8 = DailyStockPools(documents).read(sid, DAY)
        documents.put("strategy_overrides", sid, {"basic_filter": {"float_cap_max": 11e9}})
        service.daily_pools(documents, snapshot, None, wall(8, 1), None)
        assert len(calls) == 1
        service.daily_pools(documents, snapshot, None, wall(9), None)
        assert len(calls) == 2
        assert calls[1]["overrides_map"][sid]["basic_filter"]["float_cap_max"] == 11e9
        pool9 = DailyStockPools(documents).read(sid, DAY)
        assert pool9["id"] != pool8["id"]
        assert pool9["generation_slot"] == "09:00"
        assert DailyStockPools(documents).read(sid, DAY, record_id=pool8["id"]) is None
        service.daily_pools(documents, snapshot, None, wall(9, 1), None)
        assert len(calls) == 2
    finally:
        service.runtime.close()


def test_sql_partial_failure_retries_only_missing_strategies(env):
    from test_daily_pool_schedule import wall
    _, documents = owned(env)
    calls = []
    def execute(task):
        calls.append(task)
        ids = task["strategy_ids"]
        failed = ids[-1] if len(calls) == 1 else None
        return {"pools": {sid: payload(["600000.SH"], strategy_id=sid) for sid in ids if sid != failed},
                "errors": {failed: "missing reference"} if failed else {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    try:
        service.daily_pools(documents, {"frames": {}}, None, wall(8), None)
        marker = documents.get("daily_stock_pool_jobs", f"{DAY}:08:00")
        failed = next(iter(marker["errors"]))
        assert marker["state"] == "failed"
        service.daily_pools(documents, {"frames": {}}, None, wall(8, 1), None)
        assert calls[1]["strategy_ids"] == [failed]
        assert documents.get("daily_stock_pool_jobs", f"{DAY}:08:00")["state"] == "succeeded"
    finally:
        service.runtime.close()


def test_daily_pool_runs_before_settlement_without_trading_or_s02_state(env, tmp_path, monkeypatch):
    _, documents = owned(env)
    from app.multitenancy import scheduled_worker
    calls = []
    def generate(self, documents, snapshot, repo, now, claim):
        calls.append(now.date())
    monkeypatch.setattr(TenantSchedules, "daily_pools", generate, raising=False)
    monkeypatch.setattr(scheduled_worker, "build_data_fingerprint", lambda *a: pytest.fail("research run"))
    service = TenantSchedules(env["db"], None)
    env["data"].execute("UPDATE scheduler.work SET due_at=NULL WHERE kind='scheduled'")
    env["data"].execute("UPDATE scheduler.work SET due_at=now() WHERE tenant_id=%s AND kind='scheduled'",
                        (documents.context.tenant_id,))
    claim = Scheduler(env["db"]).claim("scheduled", 1)[0]
    try:
        asyncio.run(service.process(claim, {"settlement_days": []}, SimpleNamespace(
            store=SimpleNamespace(data_dir=tmp_path)), now=datetime(
                2026, 10, 26, 9, 40, tzinfo=ZoneInfo("Asia/Shanghai"))))
        assert calls == [DAY]
        assert env["data"].execute(
            "SELECT due_at FROM scheduler.work WHERE tenant_id=%s AND kind='scheduled'",
            (documents.context.tenant_id,),
        ).fetchone()["due_at"] is not None
    finally:
        service.runtime.close()


def test_sql_latest_only_is_atomic_and_tenant_isolated(env):
    from dataclasses import replace
    _, documents = owned(env)
    _, other = owned(env)
    store = DailyStockPools(documents)
    other_pool = DailyStockPools(other).save(key(), payload(["600008.SH"]), started_at=10)
    sibling = store.save(replace(key(), strategy_id="other"),
                         payload(["600009.SH"], strategy_id="other"), started_at=11)
    eight = store.save(key(), payload(["600000.SH"], generation_slot="08:00"), started_at=10)
    with pytest.raises(ValueError), documents.transaction() as unit:
        DailyStockPools(unit).save(key(), payload([], generation_slot="09:00"), started_at=20)
        raise ValueError("rollback")
    assert store.read("sample", DAY)["id"] == eight["id"]
    nine = store.save(key(), payload([], generation_slot="09:00"), started_at=20)
    assert store.read("sample", DAY, record_id=eight["id"]) is None
    assert store.read("sample", DAY)["id"] == nine["id"]
    assert store.save(key(), payload(["600000.SH"], generation_slot="08:00"), started_at=15)["id"] == nine["id"]
    assert store.read("other", DAY)["id"] == sibling["id"]
    assert DailyStockPools(other).read("sample", DAY)["id"] == other_pool["id"]


def test_sql_concurrent_successful_writes_keep_only_latest(env):
    from concurrent.futures import ThreadPoolExecutor
    _, documents = owned(env)
    store = DailyStockPools(documents)
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda sequence: store.save(
            key(), payload([f"60000{sequence}.SH"]), started_at=sequence), range(1, 9)))
    with documents.transaction(write=False) as unit:
        count = unit.connection.execute(
            "SELECT count(*) AS n FROM business.daily_stock_pools WHERE strategy_id='sample' AND day=%s",
            (DAY,)).fetchone()["n"]
    assert count == 1
    assert store.read("sample", DAY)["symbols"] == ["600008.SH"]


def test_sql_nine_partial_failure_retains_eight_until_successful_retry(env):
    from test_daily_pool_schedule import wall
    _, documents = owned(env)
    calls = []
    failed_id = []
    def execute(task):
        calls.append(task)
        ids = task["strategy_ids"]
        if len(calls) == 1:
            failed_id.append(ids[-1])
        failed = failed_id[0] if len(calls) == 2 else None
        return {"pools": {sid: payload(["600000.SH"], strategy_id=sid) for sid in ids if sid != failed},
                "errors": {failed: "missing reference"} if failed else {}}
    service = TenantSchedules(env["db"], None, executor=SimpleNamespace(execute=execute))
    try:
        service.daily_pools(documents, {"frames": {}}, None, wall(8), None)
        sid = failed_id[0]
        eight = DailyStockPools(documents).read(sid, DAY)
        service.daily_pools(documents, {"frames": {}}, None, wall(9), None)
        assert DailyStockPools(documents).read(sid, DAY)["id"] == eight["id"]
        assert DailyStockPools(documents).read(calls[0]["strategy_ids"][0], DAY)["generation_slot"] == "09:00"
        service.daily_pools(documents, {"frames": {}}, None, wall(9, 1), None)
        assert calls[2]["strategy_ids"] == [sid]
        assert DailyStockPools(documents).read(sid, DAY, record_id=eight["id"]) is None
        assert DailyStockPools(documents).read(sid, DAY)["generation_slot"] == "09:00"
    finally:
        service.runtime.close()
