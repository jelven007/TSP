"""S02可比对运行证据、独立异常观察及历史诊断溯源。

输入/结果语义与真实时钟分开; 诊断重算保留原盘中回执, 不替代原运行。
当前字段及异常阈值见docs/reference/s02.md。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta

from app.market_time import as_cn_datetime
from app.services.strategy_input_manifest import common_key, digest

SID = "custom_mvxxcp88"
_CLOCK_FIELDS = frozenset({
    "elapsed_ms", "computed_at", "updated_at", "observed_at", "entry_evaluated_at",
    "started_at", "finished_at", "completed_at", "scheduled_at", "joined_at", "next_at",
    "retry_until", "attempts", "history", "first_receipt", "first_error", "latency",
})


def _semantic(value):
    """剥离列出的非语义过程字段; 保留数据时点、价格、列表顺序及覆盖。"""
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in value.items() if key not in _CLOCK_FIELDS}
    if isinstance(value, list):
        return [_semantic(item) for item in value]
    return value


def semantic_result(daily, phase, input_sha256):
    """按所属阶段生成结果摘要; 不舍入价格、不重排顺位、不丢逐股覆盖。"""
    signals = (daily.get("monitor_tasks") or {}).get("buy", {}).get("signals", {})
    quality = {signal: {"steps": {
        key: deepcopy(step) for key, step in value.get("progress", {}).get("steps", {}).items()
        if (key == "breadth") == (phase == "breadth")}}
        for signal, value in (daily.get("entry_data_quality") or {}).items()}
    fields = {"state", "passed", "terminal", "rows", "hits", "count", "input_sha256",
              "input_excluded_symbols", "progress", "stage", "pool_total", "pool_usable",
              "pool_up_pct", "known_positive", "missing_symbols", "breadth_policy"}
    value = {"schema": "s02_semantic_result_v1", "as_of": daily["as_of"], "phase": phase,
             "input_sha256": input_sha256, "quality": quality,
             "signals": {key: {field: item[phase][field] for field in fields if field in item.get(phase, {})}
                         for key, item in signals.items()}}
    if phase == "stocks":
        value.update(rows=(daily.get("sections") or {}).get("buy", []),
                     hits=daily.get("entry_signal_hits") or [])
    return digest(_semantic(value))


def record_phase(daily, payload, phase, timing, *, dispatched_at, received_at):
    """记录真实冻结输入与传输/计算时钟; 只有正常终态才认证结果摘要。"""
    manifest = payload.get("strategy_input_manifests", {}).get(SID, {}).get(phase)
    if not manifest or phase not in ("stocks", "breadth"):
        return
    signals = daily.get("monitor_tasks", {}).get("buy", {}).get("signals", {})
    owned = [item.get(phase, {}) for item in signals.values()]
    for step in owned:
        step.update(input_state="ready", input_key=common_key(manifest["input"]),
                    input_sha256=manifest["input_sha256"], input_reason=None)
    evidence = daily.setdefault("run_evidence", {
        "schema": "s02_run_evidence_v1", "as_of": daily["as_of"], "phases": {}})
    normal = bool(owned) and all(step.get("terminal") and step.get("state") in (
        "success", "rejected") for step in owned)
    evidence["phases"][phase] = {
        "input_key": common_key(manifest["input"]), "input_sha256": manifest["input_sha256"],
        "definition_sha256": manifest["input"]["definition_sha256"],
        "algorithm_sha256": manifest["input"]["algorithm_sha256"],
        "result_sha256": semantic_result(daily, phase, manifest["input_sha256"]) if normal else None,
        "state": "complete" if normal else "incomplete",
        "provenance": "realtime", "timing": {
            **deepcopy(manifest.get("timing", {})), "bar_at": manifest["input"]["bar_at"],
            "publication_available_at": manifest["published_at"],
            "mirror_confirmed_at": payload.get("s02_input_status", {}).get(SID, {}).get(
                phase, {}).get("mirror_confirmed_at"),
            "scheduled_at": next((step.get("scheduled_at") for step in owned), None),
            "dispatched_at": dispatched_at, "received_at": received_at,
            "compute": deepcopy(timing),
        }}


def operational_state(payload, now, jobs):
    """根据实际回执、在途请求和输入状态识别漏触发、延迟、阻塞及过期。

    计划后5秒起观察, 派发60秒识别阻塞, 启动1分钟区分输入/计算延迟。
    正常终态不报异常; 仅展示广度按自身阶段记录, 不改买任务门控。
    """
    now = as_cn_datetime(now)
    windows = payload.get("strategy_task_windows", {}).get(SID, {}).get("buy", [])
    daily = payload.get("strategy_task_results", {}).get(SID, {})
    saved = (daily.get("monitor_tasks") or payload.get("strategy_task_receipts", {}).get(
        SID, {}).get("tasks", {})).get("buy", {}).get("signals", {})
    issues = []
    for window in windows:
        signal = window["signal"]
        params = signal.get("params") or {}
        start = datetime.combine(now.date(), datetime.fromisoformat(
            f"{now.date()}T{window['start']}").time(), now.tzinfo)
        end = min(datetime.combine(now.date(), datetime.fromisoformat(
            f"{now.date()}T{window['end']}").time(), now.tzinfo), now.replace(
                hour=14, minute=55, second=0, microsecond=0))
        phases = {"stocks": start + timedelta(minutes=float(params.get("evaluation_delay_minutes") or 0))}
        if window.get("observe_breadth") or params.get("pool_up_min_pct") is not None:
            phases["breadth"] = start + timedelta(seconds=15)
        for phase, scheduled in phases.items():
            step = saved.get(signal["id"], {}).get(phase, {})
            normal = step.get("terminal") and step.get("state") in ("success", "rejected")
            if normal:
                continue
            job = jobs.get(phase, {})
            status = payload.get("s02_input_status", {}).get(SID, {}).get(phase, {})
            kind = None
            if now > end or (phase == "breadth" and now >= end):
                kind = "expired_unfinished"
            elif now >= scheduled + timedelta(seconds=5):
                if not step and not job and not status:
                    kind = "missed_trigger"
                elif job.get("dispatched_at") and now > as_cn_datetime(
                        datetime.fromisoformat(job["dispatched_at"])) + timedelta(seconds=60):
                    kind = "stuck_compute"
                elif status.get("state") in ("invalid_input", "no_valid_input"):
                    kind = "invalid_input"
                elif now >= start + timedelta(minutes=1) and status.get("state") != "ready":
                    kind = "delayed_input"
                elif now >= start + timedelta(minutes=1):
                    kind = "delayed_compute"
            if kind:
                issues.append({"kind": kind, "phase": phase, "signal_id": signal["id"],
                               "scheduled_at": scheduled.isoformat(), "input_state": status.get("state"),
                               "reason": status.get("reason"), "first_error": step.get("first_error")})
    return {"schema": "s02_operational_state_v1", "as_of": str(now.date()),
            "generation": payload.get("strategy_task_generations", {}).get(SID),
            "runtime_identity": payload.get("daily_identities", {}).get(SID, {}).get("runtime_identity"),
            "observed_at": now.isoformat(), "issues": issues}


def review_provenance(original, reviewed):
    """诊断投影携带原盘中身份/回执, 不能用补算成功覆盖原运行状态。"""
    original = annotate_diagnostic(deepcopy(original))
    return {
        "original_realtime": deepcopy(original.get("original_realtime") or {
            "record_id": original.get("record_id"),
            "buy_status": (original.get("section_statuses") or {}).get("buy"),
            "buy_error": (original.get("section_errors") or {}).get("buy"),
            "buy_task": (original.get("monitor_tasks") or {}).get("buy"),
            "run_evidence": original.get("run_evidence"),
        }),
        "diagnostic_review": {
            "record_id": reviewed.get("record_id"), "diagnostic_only": True,
            "provenance": deepcopy(reviewed.get("repair_provenance")),
            "buy_status": (reviewed.get("section_statuses") or {}).get("buy"),
        },
    }


def manual_review(strategy_id, result, started_at):
    """S02历史日或当日截止后的API重算标为独立诊断, 保留原结果字段。"""
    from datetime import date, time

    from app.market_time import cn_now
    if (strategy_id != SID or result.get("section_mode") != "signals"
            or result.get("selection_only") or result.get("repair_provenance")):
        return result
    now = cn_now()
    day = date.fromisoformat(str(result["as_of"]))
    if day > now.date() or (day == now.date() and now.time() <= time(14, 55)):
        return result
    return {**deepcopy(result), "repair_provenance": {
        "kind": "s02_manual_signal_review_v1", "as_of": str(day),
        "diagnostic_only": True, "review_only": True,
        "reviewed_at": now.isoformat(), "started_at": started_at,
    }}


def annotate_diagnostic(result):
    """读取既有诊断修正时保留首次盘中回执, 供兼容历史档案溯源。"""
    for signal in (result.get("monitor_tasks") or {}).get("buy", {}).get("signals", {}).values():
        for phase in ("stocks", "breadth"):
            step = signal.get(phase, {})
            provenance = step.get("repair_provenance") or {}
            if not (provenance.get("review_only") or provenance.get("diagnostic_only")):
                continue
            first = step.get("first_receipt") or {}
            result.setdefault("original_realtime", {
                "record_id": result.get("record_id"), "buy_status": first.get("state"),
                "buy_error": "; ".join(first.get("errors") or []) or None,
                "first_receipt": deepcopy(first), "phase": phase,
            })
            result["diagnostic_review"] = {
                "record_id": result.get("record_id"), "diagnostic_only": True,
                "provenance": deepcopy(provenance), "buy_status": step.get("state"),
            }
    return result


def save_operational(unit, payload, now, jobs, *, observer="worker", worker_ready=True):
    """只在已发布交易日持久保存观察, 按世代/身份/阶段/类型去重首次异常。

    API与Worker可独立写入观察; 无有效Worker心跳时补充可核实的存活问题。
    此函数不采集、不执行策略、不发送外部消息。
    """
    if SID not in payload.get("strategy_task_windows", {}):
        return
    from app.market_time import published_trading_day
    if published_trading_day(as_cn_datetime(now).date()) is not True:
        return
    state = operational_state(payload, now, jobs)
    state["observer"] = observer
    if not worker_ready and state["issues"]:
        state["issues"].append({
            "kind": "worker_unavailable", "phase": "stocks", "signal_id": "worker",
            "scheduled_at": state["issues"][0]["scheduled_at"],
            "reason": "独立观察未发现有效Worker心跳"})
    key = f"{SID}:{state['as_of']}"
    previous = unit.get("strategy_operational_state", key) or {}
    # 当前状态保留本轮观察时钟, 首次异常另按身份去重; 只有问题变化才失效日视图。
    unit.put("strategy_operational_state", key, state)
    for issue in state["issues"]:
        identity = digest([key, state["generation"], state["runtime_identity"],
                           issue["signal_id"], issue["phase"], issue["kind"]])
        if not unit.get("strategy_operational_records", identity):
            unit.put("strategy_operational_records", identity, {
                **state, "issues": [issue], "first_observed_at": state["observed_at"]})
    if previous.get("issues") != state["issues"]:
        from app.services.strategy_day_views import receipt_updated
        receipt_updated(unit, SID, state["as_of"])
