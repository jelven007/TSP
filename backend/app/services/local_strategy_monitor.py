"""单机顺序监控复用阶段内核; Full 隔离编排由 Worker 经 Broker 启动任务容器。"""
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

from app.cell.context import AccessError
from app.market_time import cn_now, published_trading_day
from app.multitenancy.documents import json_value
from app.multitenancy.monitor_compute import (
    evaluate_snapshot,
    evaluate_strategy_phases_serial,
    failure_events,
)
from app.services.alert_store import append_many
from app.services.daily_selections import DailySelections
from app.services.daily_stock_pools import DailyStockPools
from app.services.strategy_monitor_state import CONTRACT, plan
from app.services.strategy_monitor_tasks import (
    _local_lock,
    assert_generation,
    fence_tasks,
    monitored_ids,
    monitoring_state,
    request_phase_repairs,
    sell_roster,
    task_receipt,
    task_windows,
    trusted_scope,
    wait_for_repairs,
)
from app.services.strategy_results import StrategyResults, capture_identity, execution_snapshot
from app.strategy.config import list_overrides
from app.strategy.daily_pool import DEFINITION_VERSION
from app.strategy.version import runtime_identity


def run(engine, service, storage, *, repair=None):
    """冻结本轮定义及按日输入, 计算后在锁内重新校验身份/世代并保存。

    单机 executor 没有 isolated 能力, 先广度后个股在本进程调用同一内核。
    已有按日归档优先于缺失回执; 归档、通知、任务状态沿本地保存边界写入。
    返回本次保存的策略ID; 保存前重新校验当前身份、启用世代及提交时钟。
    本地归档/通知/回执分步写入, 不具有Full业务库同一事务的原子提交语义。
    """
    now = cn_now()
    if published_trading_day(now.date()) is not True:
        return []
    frozen = execution_snapshot(engine, storage)
    frozen._scheduled_monitor_owner = False
    overrides = list_overrides(storage)
    previous = task_receipt(storage, "_runtime", now.date())
    payload = {"tenant_id": "standalone", "state": previous.get("state", {}),
        "rules": [], "strategy_task_ids": [], "strategy_task_plans": {}, "strategy_task_windows": {},
        "strategy_task_receipts": {}, "strategy_task_generations": {}, "strategy_task_results": {},
        "daily_identities": {}, "late_task_scopes": {}, "daily_pool_snapshots": {},
        "selection_snapshots": {}, "sell_rosters": {}, "overrides": overrides,
        "snapshot": {"frames": {}, "status": {"is_trading_hours": False}}}
    identities = {}
    for sid in monitored_ids(storage):
        if not frozen.has(sid) or frozen.get(sid).source not in ("custom", "ai"):
            continue
        strategy = frozen.get(sid)
        selected = DailySelections(storage).read(sid, now.date())
        roster = sell_roster(storage, sid, now.date())
        from app.strategy.s02_173004552 import SID as S05_ID
        if sid == S05_ID:
            from app.services.routed_strategy_monitor import task_windows as routed_windows
            windows = routed_windows(frozen, strategy, overrides.get(sid) or {}, day=now.date(),
                                     selection=selected, roster=roster, require_market_route=True)
        else:
            windows = task_windows(frozen, strategy, overrides.get(sid) or {})
        identity = capture_identity(frozen, storage, sid, overrides=overrides.get(sid),
            timeframe=(strategy.meta.get("timeframes") or ["1d"])[0])
        saved = task_receipt(storage, sid, now.date())
        if saved.get("runtime_identity") != identity.runtime_identity:
            saved = {}
        archived = StrategyResults(storage).read(identity, str(now.date()))
        daily = (archived or {}).get("result") or {}
        if daily.get("monitor_contract") == CONTRACT and daily.get("as_of") == str(now.date()):
            # 归档已成功而之后回执写入失败时, 以同日正式归档的阶段状态为准。
            saved = {**saved, "tasks": deepcopy(daily.get("monitor_tasks") or {})}
        planning = plan(windows, saved.get("tasks", {}), now, trading=True)
        if repair:
            wait_for_repairs(planning, saved, repair)
        if not planning["actions"]:
            continue
        pool = DailyStockPools(storage).snapshots([sid], now.date()).get(sid) or {}
        if pool.get("definition_version") != DEFINITION_VERSION:
            try:
                pool = trusted_scope(frozen, service.repo, storage, sid, now.date(), overrides.get(sid))
                DailyStockPools(storage).save(identity, pool, started_at=int(now.timestamp() * 1e9))
            except Exception as exc:
                pool = {"error": str(exc)}
        identities[sid] = identity
        payload["rules"].append({"id": monitoring_state(storage, sid)["rule_id"], "type": "strategy",
            "scope": "all", "asset_type": "stock", "direction": "both", "strategy_id": sid,
            "name": strategy.meta.get("name", sid), "notify_events": ["buy_signal", "sell_signal"]})
        payload["strategy_task_ids"].append(sid)
        payload["strategy_task_plans"][sid] = planning
        payload["strategy_task_windows"][sid] = windows
        payload["strategy_task_receipts"][sid] = saved
        payload["strategy_task_generations"][sid] = monitoring_state(storage, sid)["generation"]
        payload["strategy_task_results"][sid] = (archived or {}).get("result", {})
        payload["daily_identities"][sid] = asdict(identity)
        payload["late_task_scopes"][sid] = f"{sid}:{runtime_identity(strategy, overrides.get(sid) or {})}"
        payload["daily_pool_snapshots"][sid] = pool
        payload["selection_snapshots"][sid] = selected
        payload["sell_rosters"][sid] = roster
    if not identities:
        return []
    executor = SimpleNamespace(execute=lambda task: evaluate_snapshot(task, service.repo, frozen, storage))
    result = json_value(evaluate_strategy_phases_serial(payload, service.repo, executor))
    if repair:
        request_phase_repairs(payload, result["results"], repair, now)
    changed = []
    with _local_lock:
        for sid, daily in result["results"].items():
            try:
                assert_generation(storage, sid, payload["strategy_task_generations"][sid])
                current = capture_identity(engine, storage, sid,
                    overrides=list_overrides(storage).get(sid), timeframe=identities[sid].timeframe)
            except (AccessError, ValueError):
                continue
            if current.fingerprint != identities[sid].fingerprint:
                continue
            rejected = fence_tasks(daily, payload["strategy_task_windows"][sid], cn_now())
            event_kinds = {"buy_signal" if kind == "buy" else "sell_signal" for kind in rejected}
            alerts, failures = failure_events({sid: daily}, payload["rules"],
                                              task_receipt(storage, sid, now.date()).get("failures", {}), now)
            StrategyResults(storage).save(identities[sid], daily, started_at=int(now.timestamp() * 1e9))
            append_many(storage, [*alerts, *[event for event in result["events"]
                if event.get("strategy_id") == sid and event.get("type") not in event_kinds]])
            task_receipt(storage, sid, now.date(), {
                "strategy_id": sid, "as_of": str(now.date()), "contract": CONTRACT,
                "runtime_identity": identities[sid].runtime_identity,
                "tasks": deepcopy(daily["monitor_tasks"]), "failures": failures})
            changed.append(sid)
        if changed:
            task_receipt(storage, "_runtime", now.date(), {"state": result["state"]})
    return changed
