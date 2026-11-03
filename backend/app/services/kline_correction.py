"""交易日15:35修正当日五周期K线; 忙时保留请求, 终态按日期持久去重。"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import date, time

from app.config import settings
from app.market_time import cn_now
from app.services import pipeline_jobs, preferences, trading_day
from app.services.data_release import publish_data_release
from app.services.fs_utils import atomic_write_text
from app.services.repair_daily import run_repair_daily

logger = logging.getLogger(__name__)
PERIODS = ("1d", "1m", "5m", "10m", "15m")
CORRECTION_TIME = time(15, 35)
_dispatch_lock = threading.Lock()


def _load(repo) -> dict | None:
    path = repo.store.data_dir / "kline_correction" / "current.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    # Fail closed rather than repeat a successful date after corrupt storage.
    if not isinstance(state, dict) or not state.get("day") or not state.get("status"):
        raise ValueError("K线修正状态无效")
    date.fromisoformat(state["day"])
    return state


def _save(repo, state: dict) -> dict:
    root = repo.store.data_dir / "kline_correction"
    root.mkdir(parents=True, exist_ok=True)
    state = {**state, "updated_at": cn_now().isoformat()}
    text = json.dumps(state, ensure_ascii=False)
    atomic_write_text(root / f'{state["day"]}.json', text)
    atomic_write_text(root / "current.json", text)
    return state


def _allowed(now) -> bool:
    return (
        settings.service_role in {"standalone", "platform"}
        and preferences.get_data_sync_auto_enabled()
        and trading_day.is_trading_day(now) is True
    )


def request_correction(repo, capset, quote_service=None) -> dict | None:
    """固定触发点登记当天请求; 重启后仅续跑已经登记的等待请求。"""
    now = cn_now()
    if not _allowed(now) or now.time().replace(tzinfo=None) < CORRECTION_TIME:
        return None
    if not _dispatch_lock.acquire(blocking=False):
        return None
    try:
        state = _load(repo)
        if state and state["day"] == now.date().isoformat():
            return _dispatch(repo, capset, quote_service, state)
        if state and state["status"] in {"pending", "running"}:
            _save(repo, {**state, "status": "interrupted", "error": "等待请求已跨日, 未修正原交易日"})
        state = _save(repo, {"day": now.date().isoformat(), "status": "pending"})
        return _dispatch(repo, capset, quote_service, state)
    finally:
        _dispatch_lock.release()


def retry_pending(repo, capset, quote_service=None) -> dict | None:
    """只重试执行槽占用造成的等待, 失败/取消/中断均不自动重跑。"""
    if not _dispatch_lock.acquire(blocking=False):
        return None
    try:
        state = _load(repo)
        if state is None or state["status"] not in {"pending", "running"}:
            return state
        return _dispatch(repo, capset, quote_service, state)
    finally:
        _dispatch_lock.release()


def _dispatch(repo, capset, quote_service, state: dict) -> dict:
    now = cn_now()
    if state["day"] != now.date().isoformat():
        return _save(repo, {**state, "status": "interrupted", "error": "等待请求已跨日, 未修正原交易日"})
    if not _allowed(now):
        return state
    store = pipeline_jobs.job_store
    if state["status"] == "running":
        # JobStore startup reaps orphaned workers. Never restart partially written work.
        job = store.get(state["job_id"])
        status = job.get("status") if job else "interrupted"
        if status in {"succeeded", "failed", "cancelled", "interrupted"}:
            return _save(repo, {**state, "status": status, "error": (job or {}).get("error")})
        return state
    if state["status"] != "pending":
        return state
    owner = f"kline_correction:{uuid.uuid4().hex}"
    if not pipeline_jobs.try_acquire_run_slot(owner):
        return state
    try:
        job_id, is_new = store.create(
            kind="kline_correction", source="scheduled", long_running=True,
            request={"day": state["day"], "periods": list(PERIODS), "mode": "full"},
        )
        if not is_new:
            return state
        try:
            state = _save(repo, {**state, "status": "running", "job_id": job_id})
        except Exception:
            store.fail(job_id, "K线修正请求状态保存失败, 尚未开始修正")
            raise

        def check_cancelled():
            if pipeline_jobs.is_cancelled(job_id):
                raise pipeline_jobs.JobCancelledError(job_id)

        def run():
            target = date.fromisoformat(state["day"])
            results = {}
            for index, period in enumerate(PERIODS):
                check_cancelled()

                def progress(stage, pct, msg, stage_pct=None, skip_log=False, *, offset=index, label=period):
                    check_cancelled()
                    store.progress(
                        job_id, stage, min(99, (offset * 100 + pct) // len(PERIODS)),
                        f"{label}: {msg}", stage_pct=stage_pct, skip_log=skip_log,
                    )

                result = run_repair_daily(
                    repo, capset, target, target, on_progress=progress,
                    quote_service=quote_service, period=period, mode="full",
                )
                results[period] = result
                daily_skipped_or_empty = period == "1d" and (
                    "sync_daily" in result.get("skipped_stages", [])
                    or result.get("daily_rows") == 0
                )
                if (
                    result.get("error") or result.get("remaining")
                    or result.get("gap_check_warning") or daily_skipped_or_empty
                ):
                    raise RuntimeError(f"{period}修正未完成: {result}")
            check_cancelled()
            release = publish_data_release(
                repo.store.data_dir, reason="kline_correction",
                write_lock=getattr(repo, "_write_lock", None),
            )
            return {"day": state["day"], "periods": results, "data_release_id": release["release_id"]}

        try:
            result = pipeline_jobs.run_with_capacity(job_id, run)
            store.succeed(job_id, result)
        except pipeline_jobs.JobCancelledError:
            store.cancelled(job_id)
        except Exception as exc:
            logger.exception("当日K线修正失败: %s", state["day"])
            store.fail(job_id, str(exc))
        job = store.get(job_id)
        return _save(repo, {**state, "status": job["status"], "error": job.get("error")})
    finally:
        pipeline_jobs.release_run_slot(owner)
