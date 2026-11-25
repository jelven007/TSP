"""交易日15:45主动复核并修复当日遗留日线、收盘证据及加工问题。"""
from __future__ import annotations

import json
import logging
import math
import threading
import uuid
from datetime import date, datetime, time, timedelta

import polars as pl

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


def _close_review(repo, day):
    """Read only this day's evidence; keep all symbol failures, including on retry."""
    from app.services.daily_close_validation import validate_daily_close

    report = {"checked_count": 0, "valid_count": 0, "invalid_count": 0, "errors": []}
    try:
        raw, closing = repo.get_daily_close_inputs(day)
        files = sorted((repo.store.data_dir / "kline_daily_enriched" / f"date={day}").glob("*.parquet"))
        rows = pl.read_parquet(files) if files else pl.DataFrame()
        symbols = set(raw["symbol"].drop_nulls())
        report["checked_count"] = len(symbols)
        rejected = validate_daily_close(
            repo, day, rows=rows if not rows.is_empty() else None, isolate_symbols=True,
        ) or {}
        covered = set(rows["symbol"].drop_nulls()) if "symbol" in rows.columns else set()
        for symbol in symbols - covered:
            rejected.setdefault(symbol, f"{day} {symbol} 加工日线缺失")

        def values(frame, symbol, column):
            if not {"symbol", column} <= set(frame.columns):
                return []
            return [
                value if not isinstance(value, float) or math.isfinite(value) else None
                for value in frame.filter(pl.col("symbol") == symbol)[column].to_list()
            ]

        report["errors"] = [
            {"symbol": symbol, "reason": reason,
             "daily_closes": values(raw, symbol, "close"),
             "minute_closes": values(closing, symbol, "close"),
             "enriched_closes": values(rows, symbol, "raw_close" if "raw_close" in rows.columns else "close"),
             "quote_ts": values(raw, symbol, "quote_ts"),
             "quote_time_basis": values(raw, symbol, "quote_time_basis")}
            for symbol, reason in sorted(rejected.items())
        ]
        report["invalid_count"] = len(rejected)
        report["valid_count"] = len(symbols - set(rejected))
    except (OSError, ValueError, KeyError, pl.exceptions.PolarsError) as exc:
        report["error"] = str(exc)
    return report


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
        fields = ("day", "status", "job_id", "attempts", "updated_at", "error", "next_retry_at", "result",
                  "requested_at", "first_started_at", "last_started_at", "last_finished_at",
                  "last_stage", "diagnostics")
        result = state.get("result") or {}
        review = ((state.get("diagnostics") or {}).get("close_review")
                  or result.get("close_review") or {}).get("after") or {}
        gaps = result.get("gap_check") or (state.get("diagnostics") or {}).get("gap_check") or {}
        complete = (state["status"] == "succeeded" and review.get("valid_count", 0) > 0
                    and not review.get("error") and not review.get("invalid_count")
                    and not any(gaps.get(key) for key in
                                ("remaining", "error", "warning", "gap_check_warning")))
        return {**base, "available": calendar is not None, "is_trading_day": calendar,
                **{key: state[key] for key in fields if key in state},
                "completion": "complete" if complete else "partial" if
                    review.get("valid_count", 0) > 0 and not review.get("error") else "unverified",
                "coverage": {key: review.get(key) for key in
                             ("checked_count", "valid_count", "invalid_count")} | {
                                 "remaining": gaps.get("remaining"),
                                 "suspended_skipped": gaps.get("suspended_skipped")},
                "retry_eligible": bool(base["enabled"] and calendar is True
                                       and state["day"] == str(now.date())
                                       and state["status"] in {"failed", "interrupted"})}
    except (OSError, ValueError, KeyError, TypeError):
        logger.exception("读取盘后遗留复核状态失败")
        return {**base, "available": False, "status": "unavailable",
                "error": "复核状态读取失败，请检查服务日志"}  # noqa: RUF001


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
                             "error": "复核请求已跨日，原交易日未完成，不自动重放历史"})  # noqa: RUF001
            state = None
        if state is None:
            state = _save(repo, {"day": str(day), "status": "pending", "attempts": 0,
                                 "requested_at": now.isoformat()})
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
                store.fail(job_id, "盘后复核状态保存失败，尚未开始写入")  # noqa: RUF001
                raise

            def check_cancelled():
                if pipeline_jobs.is_cancelled(job_id) or (stop is not None and stop.is_set()):
                    raise pipeline_jobs.JobCancelledError(job_id)
                current = cn_now()
                if current.date() != day or not _allowed(current):
                    raise ValueError("当日盘后复核窗口已结束或自动同步已暂停")

            def progress(stage, pct, msg, stage_pct=None, skip_log=False):
                check_cancelled()
                state["last_stage"] = stage
                store.progress(job_id, stage, min(99, pct), msg,
                               stage_pct=stage_pct, skip_log=skip_log)

            attempt_diagnostics = {}

            def run():
                nonlocal state
                check_cancelled()
                started = cn_now().isoformat()
                state = _save(repo, {**state, "first_started_at": state.get("first_started_at") or started,
                                     "last_started_at": started, "last_stage": "review_close"})
                review = attempt_diagnostics["close_review"] = {"before": _close_review(repo, day)}
                state = _save(repo, {**state, "diagnostics": attempt_diagnostics})
                try:
                    return repair_inputs(
                        repo, capset, day, progress=progress, check_cancelled=check_cancelled,
                        quote_service=quote_service, previous=state if state.get("result") else None,
                        diagnostics=attempt_diagnostics,
                    )
                finally:
                    review["after"] = _close_review(repo, day)
                    state["last_finished_at"] = cn_now().isoformat()

            try:
                result = pipeline_jobs.run_with_capacity(job_id, run)
                check_cancelled()
                result = {**result, "close_review": attempt_diagnostics["close_review"]}
                state = {**state, "result": result, "input_signature": input_signature(repo, day)}
                gaps = result.get("gap_check") or {}
                after = attempt_diagnostics["close_review"]["after"]
                if after.get("error") or after["invalid_count"] or not after["valid_count"]:
                    store.fail(job_id, "收盘证据仍有异常或未核实项, 将在当日自动重试")
                elif any(gaps.get(key) for key in ("remaining", "error", "warning", "gap_check_warning")):
                    store.fail(job_id, "日线仍有缺口或未核实项，将在当日自动重试；已验证数据保持")  # noqa: RUF001
                else:
                    store.succeed(job_id, result)
            except pipeline_jobs.JobCancelledError:
                store.cancelled(job_id)
            except Exception as exc:
                logger.exception("当日盘后遗留复核失败: %s", day)
                store.fail(job_id, str(exc))
            job = store.get(job_id)
            state = {**state, "status": job["status"], "error": job.get("error"),
                     "diagnostics": attempt_diagnostics}
            if state["status"] in {"failed", "interrupted"}:
                delay = min(300, 60 * 2 ** min(3, state["attempts"] - 1))
                state["next_retry_at"] = (cn_now() + timedelta(seconds=delay)).isoformat()
            return _save(repo, state)
        finally:
            pipeline_jobs.release_run_slot(owner)
    finally:
        _lock.release()
