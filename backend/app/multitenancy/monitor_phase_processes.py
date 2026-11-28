"""按策略/阶段向 Broker 派发任务; 收齐本轮回执后汇合, Worker 不执行行情 IO。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import timedelta

from app.market_time import cn_now

SCOPED_MAPS = (
    "strategy_task_plans", "strategy_task_windows", "strategy_task_receipts",
    "strategy_task_results", "daily_pool_snapshots", "daily_identities",
    "late_task_scopes", "selection_snapshots", "sell_rosters", "previous_states",
    "positions", "overrides", "overrides_map", "params_map", "late_task_definitions",
    "strategy_input_manifests", "s02_input_status", "strategy_minute_packets",
)
PAIR_STATE = ("last_fire", "strategy_pools", "strategy_signal_state",
              "strategy_signal_seen", "sector_condition_state", "abnormal_condition_state")


def scoped(payload, sid, phase):
    """复制该策略所属输入与动作, 不修改父payload的其他策略任务。

    可信广度移除用户源码、覆盖、信号和因子注册; 广度实际保存信号仍在
    strategy_task_windows中, 完整池与冻结预选分别保留在各自输入映射。
    """
    task = deepcopy(payload)
    task.update(strategy_task_ids=[sid], late_task_strategy_ids=[], late_task_only=True,
                monitor_phase=phase)
    for key in SCOPED_MAPS:
        if key in task:
            task[key] = {sid: task[key][sid]} if sid in task[key] else {}
    task["rules"] = [rule for rule in task["rules"] if rule.get("strategy_id") == sid]
    task["sources"] = [source for source in task.get("sources", []) if source["id"] == sid]
    actions = task["strategy_task_plans"][sid]["actions"]
    task["strategy_task_plans"][sid]["actions"] = [
        action for action in actions
        if (action["phase"] == "breadth") == (phase == "breadth")]
    if phase == "breadth":
        task.pop("strategy_minute_packets", None)
        task.update(kind="monitor_breadth", sources=[], signals=[], factors=[], overrides={})
    return task


def merge_state(output, computed, task, phase):
    """仅合并该策略所属阶段及规则键, 迟到侧不能覆盖另一侧已冻结的终态。"""
    target = output["state"].setdefault("engine", {})
    incoming = (computed.get("state") or {}).get("engine") or {}
    scopes = [str(value) + ":" for value in task.get("late_task_scopes", {}).values()]
    late = target.setdefault("late_session_tasks", {})
    for key, value in incoming.get("late_session_tasks", {}).items():
        if not any(key.startswith(scope) for scope in scopes):
            continue
        saved = late.setdefault(key, {})
        saved.update(deepcopy({k: v for k, v in value.items() if k != "independent_v1"}))
        if phase in ("breadth", "stocks"):
            own = (value.get("independent_v1") or {}).get(phase)
            if own is not None:
                saved.setdefault("independent_v1", {})[phase] = deepcopy(own)
    if phase == "breadth":
        return
    rule_ids = {rule["id"] for rule in task["rules"]}
    for key in PAIR_STATE:
        existing = {tuple(pair[0]): pair for pair in target.get(key, [])}
        existing = {key: value for key, value in existing.items() if key[0] not in rule_ids}
        for pair in incoming.get(key, []):
            if pair[0][0] in rule_ids:
                existing[tuple(pair[0])] = deepcopy(pair)
        target[key] = list(existing.values())


def merge_daily(previous, calculated, phase, actions):
    """返回按阶段所有权合并的日结果副本, 不把完成顺序当作字段覆盖权。

    仅同一交易日保留阶段回执; 广度只替换自身回执, 显式过期动作除外。
    最终买状态稍后统一汇合, 本函数不直接发布候选。
    """
    if phase == "serial":
        return deepcopy(calculated)
    if previous.get("as_of") != calculated.get("as_of"):
        previous = {}
    if phase != "breadth":
        # 个股/卖结果持有其余字段; 保留已合并广度, 显式过期动作才可替换该侧。
        daily = deepcopy(calculated)
        if previous.get("run_evidence"):
            daily["run_evidence"] = deepcopy(previous["run_evidence"])
        old = (previous.get("monitor_tasks") or {}).get("buy", {}).get("signals", {})
        expired = {a["signal_id"] for a in actions if a.get("expired_phase") == "breadth"}
        for signal, value in daily.get("monitor_tasks", {}).get("buy", {}).get("signals", {}).items():
            if signal not in expired and "breadth" in old.get(signal, {}):
                value["breadth"] = deepcopy(old[signal]["breadth"])
        return daily
    daily = deepcopy(previous or calculated)
    task = daily.setdefault("monitor_tasks", {}).setdefault("buy", {})
    signals = task.setdefault("signals", {})
    incoming = calculated.get("monitor_tasks", {}).get("buy", {}).get("signals", {})
    for action in actions:
        signal = action["signal_id"]
        if "breadth" in incoming.get(signal, {}):
            signals.setdefault(signal, {})["breadth"] = deepcopy(incoming[signal]["breadth"])
    return daily


def evaluate_processes(payload, repo, executor):
    """按策略提交阶段容器请求, 收齐本轮真实回执后按状态所有权汇合。

    首批阶段共用派发后 60 秒的绝对期限, 排队及计算还受各动作窗口约束。
    客户端清理等待和后续 joined 通知请求不属于该期限, 不能据此承诺整轮
    60 秒内发布。缺回执或抛错只生成该侧失败; Broker 回收该任务容器。
    """
    from app.multitenancy.monitor_compute import (
        evaluate_strategy_phases_serial,
        failed_phase_results,
    )
    from app.services.strategy_monitor_state import independent_buy
    from app.services.strategy_monitor_tasks import apply_independent_buy
    from app.strategy.monitor import MonitorRuleEngine

    output = {"events": [], "execution_events": [], "results": {},
              "state": deepcopy(payload.get("state") or {})}
    if not output["state"].get("engine"):
        output["state"]["engine"] = MonitorRuleEngine().export_state()
    # 编排状态保留全部规则身份, 各阶段沙箱只看到所属策略规则。
    output["state"]["engine"]["rules"] = deepcopy(payload["rules"])
    tasks = []
    deadline = (cn_now() + timedelta(seconds=60)).isoformat()
    for phase in ("stocks", "breadth"):
        for sid in payload["strategy_task_ids"]:
            if not independent_buy(payload["strategy_task_windows"][sid].get("buy", [])):
                if phase == "stocks":
                    task = scoped(payload, sid, phase)
                    task["phase_deadline"] = deadline
                    task["strategy_task_plans"][sid] = deepcopy(payload["strategy_task_plans"][sid])
                    tasks.append((sid, "serial", task))
                continue
            task = scoped(payload, sid, phase)
            task["phase_deadline"] = deadline
            if task["strategy_task_plans"][sid]["actions"]:
                task["state"] = deepcopy(output["state"])
                tasks.append((sid, phase, task))
    if not tasks:
        return output

    def execute(task):
        try:
            result = executor.execute(task)
            sid = task["strategy_task_ids"][0]
            if sid not in result.get("results", {}):
                raise ValueError("计算未返回策略阶段回执")
            return result
        except Exception as exc:
            return {"events": [], "execution_events": [], "state": {},
                    "_compute_timing": getattr(exc, "compute_timing", {}),
                    "results": failed_phase_results(task, str(exc), cn_now())}

    def execute_phase(task, phase):
        if phase != "serial":
            return execute(task)
        try:
            return evaluate_strategy_phases_serial(task, repo, executor)
        except Exception as exc:
            return {"events": [], "execution_events": [], "state": {},
                    "results": failed_phase_results(task, str(exc), cn_now())}

    # 线程只并发传输请求, 行情 IO 和计算分别在 Broker 启动的任务容器;
    # 先提交个股再提交广度, 两侧输入独立, 最终返回仍须收齐本轮 futures。
    with ThreadPoolExecutor(max_workers=min(16, len(tasks)), thread_name_prefix="monitor-phase") as pool:
        futures = {pool.submit(execute_phase, task, phase): (sid, phase, task) for sid, phase, task in tasks}
        for future in as_completed(futures):
            sid, phase, task = futures[future]
            computed = future.result()
            merge_state(output, computed, task, phase)
            previous = output["results"].get(sid) or payload.get("strategy_task_results", {}).get(sid) or {}
            output["results"][sid] = merge_daily(
                previous, computed["results"][sid], phase, task["strategy_task_plans"][sid]["actions"])
            output["events"].extend(computed.get("events", []))
            output["execution_events"].extend(computed.get("execution_events", []))
            timing = computed.get("_compute_timing")
            if timing:
                output.setdefault("_compute_phases", {}).setdefault(sid, {})[phase] = timing
    # 空广度下限不门控买入, 但本轮仍等已提交广度返回; 延迟可能耗尽提交窗口。
    for sid, daily in output["results"].items():
        daily = apply_independent_buy(daily, payload["strategy_task_windows"][sid], cn_now())
        output["results"][sid] = daily
        prior = (payload.get("strategy_task_results", {}).get(sid) or {}).get("monitor_tasks", {})
        old = prior.get("buy", {}).get("signals", {})
        joined = [signal for signal, value in daily["monitor_tasks"].get("buy", {}).get("signals", {}).items()
                  if value.get("joined_at") and not old.get(signal, {}).get("joined_at")]
        emitted = (daily.get("monitor_executed_signals") or {}).get("buy", [])
        if not joined or not daily.get("entry_signal_hits") or any(
            event.get("strategy_id") == sid and event.get("type") == "buy_signal"
            for event in output["events"]
        ):
            continue
        # 无动作 joined 请求复用已完成名单和通知去重, 不重新加载预选分钟。
        # 此请求没有首批 phase_deadline, 使用独立默认预算; joined_at 防晚到观测重复通知。
        task = scoped(payload, sid, "stocks")
        task["strategy_task_plans"][sid]["actions"] = []
        task["strategy_task_receipts"][sid] = {"tasks": deepcopy(daily["monitor_tasks"])}
        task["strategy_task_results"][sid] = deepcopy(daily)
        task["state"] = deepcopy(output["state"])
        task["monitor_phase"] = "joined"
        computed = execute(task)
        merge_state(output, computed, task, "joined")
        output["events"].extend(computed.get("events", []))
        output["execution_events"].extend(computed.get("execution_events", []))
        daily.setdefault("monitor_executed_signals", {})["buy"] = sorted(set(emitted) | set(joined))
        daily["monitor_tasks"]["buy"]["executed_signals"] = joined
    output["_compute_timing"] = {"phases": output.get("_compute_phases", {})}
    return output
