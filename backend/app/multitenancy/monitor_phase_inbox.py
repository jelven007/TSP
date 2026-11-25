"""隔离阶段的唯一编排: 传输线程发Compute请求, 租户Worker收取及提交后确认。

收取不等待未完成Future; 已完成回执在事务回滚后仍保留供重试。
当前S02调度及所有权边界见docs/reference/s02.md。
"""
from __future__ import annotations

import inspect
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta

from app.market_time import cn_now
from app.multitenancy.monitor_phase_processes import merge_daily, merge_state, scoped
from app.services.strategy_input_manifest import digest


@dataclass
class Pending:
    """在途请求的租户/策略/阶段、配置签名、冻结任务和取消令牌。"""
    tenant: str
    sid: str
    phase: str
    signature: str
    task: dict
    future: object
    cancel: threading.Event


class PhaseInbox:
    """同一逻辑阶段只保留一个在途请求, 传输线程不写业务状态。

    完成Future保留到Worker事务提交; 收取时只合并本侧所属字段到最新状态。
    本侧此前提交不使另一侧失效, 外部配置/世代变化则取消旧请求。
    """

    def __init__(self, concurrency=16):
        self.pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="monitor-receipt")
        self.lock = threading.RLock()
        self.jobs = {}

    def close(self):
        """取消在途任务并关闭传输池, 退出时等待请求回收。"""
        with self.lock:
            for job in self.jobs.values():
                job.cancel.set()
            self.jobs.clear()
        self.pool.shutdown(wait=True, cancel_futures=True)

    def keys(self):
        """返回当前请求键副本, 避免调用者遍历可变内部字典。"""
        with self.lock:
            return tuple(self.jobs)

    def pending(self, tenant):
        """判断租户是否仍有待收取或待确认回执, 供Worker后续tick继续推进。"""
        with self.lock:
            return any(job.tenant == tenant for job in self.jobs.values())

    def evidence(self, tenant, sid):
        """只暴露派发时刻和Future完成事实, 不据此推断业务已提交。"""
        with self.lock:
            return {job.phase: {"dispatched_at": job.task.get("_dispatched_at"),
                                "done": job.future.done()}
                    for job in self.jobs.values() if job.tenant == tenant and job.sid == sid}

    @staticmethod
    def unpublished(payload):
        """从持久任务恢复已汇合但未发布的门控信号, 支持传输进程重启。"""
        return {
            sid: [key for key, value in (daily.get("monitor_tasks") or {}).get("buy", {}).get("signals", {}).items()
                  if value.get("joined_at") and value.get("passed") is True
                  and value.get("state") == "success" and value.get("breadth_required")
                  and value.get("publication_state") != "complete"]
            for sid, daily in payload.get("strategy_task_results", {}).items()
            if sid in payload.get("strategy_task_windows", {})
        }

    @staticmethod
    def _owned_finished(payload, job):
        """本侧动作已有终态时停止旧在途请求, 不替换其他侧终态。"""
        from app.services.strategy_monitor_state import finished
        if job.phase not in ("stocks", "breadth"):
            return False
        daily = payload.get("strategy_task_results", {}).get(job.sid) or {}
        signals = (daily.get("monitor_tasks") or {}).get("buy", {}).get("signals", {})
        actions = job.task["strategy_task_plans"][job.sid]["actions"]
        return bool(actions) and all(
            finished(signals.get(action["signal_id"], {}).get(
                action.get("expired_phase", action["phase"]), {})) for action in actions)

    @staticmethod
    def _signature(payload, sid):
        """跨日、配置、定义或监控世代变化使旧请求失效, 行情更新不在此混入。"""
        return digest([
            payload.get("_configuration_fingerprint") or [
                payload.get("sources"), payload.get("overrides"), payload.get("signals")],
            payload.get("strategy_task_generations", {}).get(sid),
            payload.get("daily_identities", {}).get(sid),
            str(cn_now().date()),
        ])

    def _dispatch(self, payload, sid, phase, task, repo, executor):
        """按逻辑动作去重后派发, 异常转换为所属阶段回执, 禁止空返回冒充成功。"""
        from app.multitenancy.monitor_compute import failed_phase_results

        signature = self._signature(payload, sid)
        actions = task["strategy_task_plans"][sid]["actions"]
        identity = [
            {key: action.get(key) for key in
             ("kind", "signal_id", "phase", "expired_phase", "scheduled_at", "retry_until")}
            for action in actions]
        key = digest([payload["tenant_id"], sid, phase, signature, identity])
        if key in self.jobs:
            return
        cancel = threading.Event()
        task["_dispatched_at"] = cn_now().isoformat()
        task["phase_deadline"] = (cn_now() + timedelta(seconds=60)).isoformat()

        def execute():
            try:
                if phase == "serial":
                    from app.multitenancy.monitor_compute import evaluate_strategy_phases_serial
                    result = evaluate_strategy_phases_serial(task, repo, executor)
                else:
                    parameters = inspect.signature(executor.execute).parameters.values()
                    cancellable = any(p.name == "cancel" or p.kind == p.VAR_KEYWORD for p in parameters)
                    result = executor.execute(task, **({"cancel": cancel} if cancellable else {}))
                if sid not in result.get("results", {}):
                    raise ValueError("计算未返回策略阶段回执")
                return result
            except Exception as exc:
                return {"events": [], "execution_events": [], "state": {},
                        "_phase_error": str(exc),
                        "_compute_timing": getattr(exc, "compute_timing", {}),
                        "results": failed_phase_results(task, str(exc), cn_now())}

        self.jobs[key] = Pending(payload["tenant_id"], sid, phase, signature, deepcopy(task),
                                 self.pool.submit(execute), cancel)

    def advance(self, payload, repo, executor):
        """派发到点任务, 只收取已完成回执; 未完成请求留给下次tick。"""
        from app.services.strategy_monitor_state import independent_buy
        from app.services.strategy_monitor_tasks import apply_independent_buy
        from app.strategy.monitor import MonitorRuleEngine

        output = {"events": [], "execution_events": [], "results": {},
                  "state": deepcopy(payload.get("state") or {}), "_phase_ack": []}
        if not output["state"].get("engine"):
            output["state"]["engine"] = MonitorRuleEngine().export_state()
        output["state"]["engine"]["rules"] = deepcopy(payload["rules"])
        tenant = payload["tenant_id"]
        with self.lock:
            for key, job in list(self.jobs.items()):
                if job.tenant != tenant:
                    continue
                if (job.sid not in payload.get("strategy_task_windows", {})
                        or job.signature != self._signature(payload, job.sid)
                        or self._owned_finished(payload, job)):
                    job.cancel.set()
                    del self.jobs[key]
            # 个股先派发; 前轮广度仍在途时也可使用Broker为个股保留的槽位。
            for sid in payload.get("strategy_task_ids", []):
                windows = payload["strategy_task_windows"][sid]
                if not independent_buy(windows.get("buy", [])):
                    task = scoped(payload, sid, "stocks")
                    task["strategy_task_plans"][sid] = deepcopy(payload["strategy_task_plans"][sid])
                    self._dispatch(payload, sid, "serial", task, repo, executor)
                    continue
                for phase in ("stocks", "breadth"):
                    task = scoped(payload, sid, phase)
                    if task["strategy_task_plans"][sid]["actions"]:
                        status = payload.get("s02_input_status", {}).get(sid, {}).get(phase)
                        if status and status["state"] != "ready":
                            from app.services.strategy_input_binding import defer_input
                            waiting = defer_input(payload, sid, phase, cn_now())
                            previous = (output["results"].get(sid)
                                        or payload.get("strategy_task_results", {}).get(sid) or {})
                            output["results"][sid] = merge_daily(
                                previous, waiting["results"][sid], phase,
                                task["strategy_task_plans"][sid]["actions"])
                            continue
                        self._dispatch(payload, sid, phase, task, repo, executor)
            for key, job in list(self.jobs.items()):
                if job.tenant != tenant or not job.future.done():
                    continue
                # 不调用未完成Future.result; 合并只更新阶段所属状态, 随后等事务ack。
                computed = deepcopy(job.future.result())
                merge_state(output, computed, job.task, job.phase)
                previous = (output["results"].get(job.sid)
                            or payload.get("strategy_task_results", {}).get(job.sid) or {})
                daily = merge_daily(previous, computed["results"][job.sid], job.phase,
                                    job.task["strategy_task_plans"][job.sid]["actions"])
                if job.phase != "serial":
                    daily = apply_independent_buy(
                        daily, payload["strategy_task_windows"][job.sid], cn_now())
                from app.services.s02_run_evidence import SID, record_phase
                if job.sid == SID:
                    record_phase(daily, job.task, job.phase, {
                        **(computed.get("_compute_timing") or {}), "runtime": computed.get("_compute_runtime")},
                                 dispatched_at=job.task["_dispatched_at"], received_at=cn_now().isoformat())
                if ((job.phase == "joined" and not computed.get("_phase_error"))
                        or any(event.get("type") == "buy_signal" and event.get("strategy_id") == job.sid
                               for event in computed.get("events", []) + computed.get("execution_events", []))):
                    for value in daily.get("monitor_tasks", {}).get("buy", {}).get("signals", {}).values():
                        if value.get("joined_at") and value.get("passed"):
                            value["publication_state"] = "complete"
                output["results"][job.sid] = daily
                output["events"].extend(computed.get("events", []))
                output["execution_events"].extend(computed.get("execution_events", []))
                output["_phase_ack"].append(key)
                output.setdefault("_compute_phases", {}).setdefault(job.sid, {})[job.phase] = (
                    computed.get("_compute_timing") or {})
            current = {**payload, "strategy_task_results": {
                **payload.get("strategy_task_results", {}), **output["results"]}}
            # 必需阶段汇合后可从持久回执续发通知; joined不带动作, 不重读分钟。
            for sid, joined in self.unpublished(current).items():
                daily = current["strategy_task_results"][sid]
                if joined and daily.get("entry_signal_hits"):
                    task = scoped(payload, sid, "stocks")
                    task["strategy_task_plans"][sid]["actions"] = []
                    task["strategy_task_receipts"][sid] = {"tasks": deepcopy(daily["monitor_tasks"])}
                    task["strategy_task_results"][sid] = deepcopy(daily)
                    task["state"] = deepcopy(output["state"])
                    task["monitor_phase"] = "joined"
                    self._dispatch(payload, sid, "joined", task, repo, executor)
        output["_compute_timing"] = {"phases": output.get("_compute_phases", {})}
        return output

    def ack(self, keys):
        """仅在归档、任务、事件及运行状态的同一事务成功提交后移除回执。"""
        with self.lock:
            for key in keys:
                self.jobs.pop(key, None)
