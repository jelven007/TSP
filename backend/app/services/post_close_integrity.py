"""交易日15:45主动复核并修复当日遗留日线、收盘证据及加工问题。"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import date, datetime, time, timedelta

from app.config import settings
from app.market_time import cn_now
from app.services import pipeline_jobs, preferences, trading_day
from app.services.fs_utils import atomic_write_text
from app.services.selection_daily_repair import input_signature, repair_inputs

logger = logging.getLogger(__name__)
REVIEW_TIME = time(15, 45)
_lock = threading.Lock()
_TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_STATES = _TERMINAL | {"pending", "running"}


def _load(repo):
    path = repo.store.data_dir / "post_close_integrity" / "current.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or state.get("status") not in _STATES:
        raise ValueError("盘后遗留复核状态无效")
    date.fromisoformat(state["day"])
    return state


def _save(repo, state):
    root = repo.store.data_dir / "post_close_integrity"
    root.mkdir(parents=True, exist_ok=True)
    state = {**state, "updated_at": cn_now().isoformat()}
    text = json.dumps(state, ensure_ascii=False, allow_nan=False)
    atomic_write_text(root / f'{state["day"]}.json', text)
    atomic_write_text(root / "current.json", text)
    return state


def _allowed(now):
    return (settings.service_role in {"platform", "standalone"}
            and preferences.get_data_sync_auto_enabled()
            and now.time().replace(tzinfo=None) >= REVIEW_TIME
            and trading_day.is_trading_day(now) is True)


def _reconcile(state):
    if state and state["status"] == "running":
        job = pipeline_jobs.job_store.get(state["job_id"])
        status = (job or {}).get("status", "interrupted")
        if status in _TERMINAL:
            return {**state, "status": status, "error": (job or {}).get("error")}
    return state


def summary(repo, *, scheduler=None):
    """Bounded pure read for the existing admin job list, without input scans."""
    now = cn_now()
    base = {"scheduled_time": "15:45", "timezone": "Asia/Shanghai",
            "today": str(now.date()), "enabled": preferences.get_data_sync_auto_enabled()}
    if scheduler is not None:
        cron = scheduler.get_job("post_close_integrity")
        retry = scheduler.get_job("post_close_integrity_retry")
        base["schedule_registered"] = cron is not None and retry is not None
        next_run = getattr(cron, "next_run_time", None)
        base["next_run_at"] = next_run.isoformat() if next_run is not None else None
    try:
        calendar = trading_day.is_trading_day(now)
        state = _reconcile(_load(repo))
        if state is None:
            return {**base, "available": calendar is not None, "is_trading_day": calendar,
                    "day": str(now.date()), "status": "not_run" if calendar is not None else "unavailable"}
        fields = ("day", "status", "job_id", "attempts", "updated_at", "error", "next_retry_at", "result")
        return {**base, "available": calendar is not None, "is_trading_day": calendar,
                **{key: state[key] for key in fields if key in state}}
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("读取盘后遗留复核状态失败")
        return {**base, "available": False, "status": "unavailable",
                "error": "复核状态读取失败, 请检查服务日志"}


def request_repair(repo, capset, quote_service=None, *, stop=None):
    """Cron and retry share the same day, calendar, switch and single-writer gates."""
    now = cn_now()
    if not _allowed(now) or (stop is not None and stop.is_set()):
        return None
    if not _lock.acquire(blocking=False):
        return None
    try:
        day = now.date()
        state = _load(repo)
        if state is not None and state["day"] != str(day):
            if state["status"] in {"pending", "running"}:
                _save(repo, {**state, "status": "interrupted",
                             "error": "复核请求已跨日, 原交易日未完成, 不自动重放历史"})
            state = None
        if state is None:
            state = _save(repo, {"day": str(day), "status": "pending", "attempts": 0})
        reconciled = _reconcile(state)
        if reconciled != state:
            state = _save(repo, reconciled)
        if state["status"] in {"running", "cancelled"}:
            return state
        if state["status"] == "succeeded" and state.get("input_signature") == input_signature(repo, day):
            return state
        if state.get("next_retry_at") and datetime.fromisoformat(state["next_retry_at"]) > now:
            return state
        owner = f"post_close_integrity:{uuid.uuid4().hex}"
        if not pipeline_jobs.try_acquire_run_slot(owner):
            return state
        store = pipeline_jobs.job_store
        try:
            job_id, created = store.create(
                kind="post_close_integrity", source="scheduled", long_running=True,
                request={"day": str(day), "purpose": "post_close_integrity", "scheduled_time": "15:45"},
            )
            if not created:
                return state
            state = {**state, "status": "running", "job_id": job_id,
                     "attempts": state.get("attempts", 0) + 1, "error": None, "next_retry_at": None}
            try:
                state = _save(repo, state)
            except Exception:
                store.fail(job_id, "盘后复核状态保存失败, 尚未开始写入")
                raise

            def check_cancelled():
                if pipeline_jobs.is_cancelled(job_id) or (stop is not None and stop.is_set()):
                    raise pipeline_jobs.JobCancelledError(job_id)
                current = cn_now()
                if current.date() != day or not _allowed(current):
                    raise ValueError("当日盘后复核窗口已结束或自动同步已暂停")

            def progress(stage, pct, msg, stage_pct=None, skip_log=False):
                check_cancelled()
                store.progress(job_id, stage, min(99, pct), msg,
                               stage_pct=stage_pct, skip_log=skip_log)

            try:
                result = pipeline_jobs.run_with_capacity(job_id, lambda: repair_inputs(
                    repo, capset, day, progress=progress, check_cancelled=check_cancelled,
                    quote_service=quote_service, previous=state if state.get("result") else None,
                ))
                check_cancelled()
                state = {**state, "result": result, "input_signature": input_signature(repo, day)}
                gaps = result.get("gap_check") or {}
                if any(gaps.get(key) for key in ("remaining", "error", "warning", "gap_check_warning")):
                    store.fail(job_id, "日线仍有缺口或未核实项, 将在当日自动重试; 已验证数据保持")
                else:
                    store.succeed(job_id, result)
            except pipeline_jobs.JobCancelledError:
                store.cancelled(job_id)
            except Exception as exc:
                logger.exception("当日盘后遗留复核失败: %s", day)
                store.fail(job_id, str(exc))
            job = store.get(job_id)
            state = {**state, "status": job["status"], "error": job.get("error")}
            if state["status"] in {"failed", "interrupted"}:
                delay = min(300, 60 * 2 ** min(3, state["attempts"] - 1))
                state["next_retry_at"] = (cn_now() + timedelta(seconds=delay)).isoformat()
            return _save(repo, state)
        finally:
            pipeline_jobs.release_run_slot(owner)
    finally:
        _lock.release()
