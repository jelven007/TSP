"""启动及交易日15:35仅补当日五周期缺口, 两种触发独立记录。"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import date, datetime, time, timedelta

from app.config import settings
from app.market_time import cn_now
from app.services import pipeline_jobs, preferences, trading_day
from app.services.data_release import publish_data_release
from app.services.fs_utils import atomic_write_text
from app.services.repair_daily import run_repair_daily

logger = logging.getLogger(__name__)
PERIODS = ("1m", "5m", "10m", "15m", "1d")
SYNC_TIME = time(15, 35)
_dispatch_lock = threading.Lock()


def finalize_daily_settlement(repo, capset, day, check_cancelled=lambda: None):
    """Publish daily readiness only after source clocks, auction and enriched agree."""
    import polars as pl

    from app.indicators.pipeline import run_pipeline
    from app.jobs.daily_pipeline import mark_settlement_ready
    from app.services import data_integrity, kline_sync
    from app.services.daily_close_validation import require_final_daily_rows, validate_daily_close

    raw, _ = repo.get_daily_close_inputs(day)
    provisional = require_final_daily_rows(raw, day, isolate_symbols=True)
    if provisional:
        # Missing-only collection preserves completed rows. Provisional quote rows
        # still need the existing official daily source before they can be finalized.
        kline_sync.sync_and_persist_daily_batch(
            sorted(provisional), repo, capset,
            start_date=datetime.combine(day, time.min),
            end_date=datetime.combine(day, time.max),
        )
    check_cancelled()
    validate_daily_close(repo, day)
    raw, _ = repo.get_daily_close_inputs(day)
    path = repo.store.data_dir / "kline_daily_enriched" / f"date={day}"

    def check_enriched():
        files = sorted(path.glob("*.parquet"))
        if not files:
            raise ValueError(f"{day}加工日线尚未生成")
        rows = pl.read_parquet(files)
        if set(raw["symbol"]) - set(rows["symbol"]):
            raise ValueError(f"{day}加工日线覆盖不完整")
        validate_daily_close(repo, day, rows=rows)

    try:
        check_enriched()
    except (OSError, ValueError):
        data_integrity.prune_enriched_partitions(repo.store.data_dir, day, "kline_daily_enriched", end=day)
        run_pipeline(data_dir=repo.store.data_dir, new_dates_only=True)
        check_enriched()
        repo.refresh_cache()
    check_cancelled()
    release = publish_data_release(
        repo.store.data_dir, reason="post_close_daily_settlement",
        write_lock=getattr(repo, "_write_lock", None),
    )
    check_cancelled()
    mark_settlement_ready(repo.store.data_dir, day, release["release_id"])
    return release


def _load(repo, *, trigger="scheduled") -> dict | None:
    filename = "startup-current.json" if trigger == "startup" else "current.json"
    path = repo.store.data_dir / "post_close_sync" / filename
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    # Fail closed rather than repeat a successful date after corrupt storage.
    if not isinstance(state, dict) or not state.get("day") or not state.get("status"):
        raise ValueError("盘后补缺状态无效")
    date.fromisoformat(state["day"])
    return state


def _save(repo, state: dict) -> dict:
    root = repo.store.data_dir / "post_close_sync"
    root.mkdir(parents=True, exist_ok=True)
    state = {**state, "updated_at": cn_now().isoformat()}
    text = json.dumps(state, ensure_ascii=False)
    prefix = "startup-" if state.get("trigger") == "startup" else ""
    atomic_write_text(root / f'{prefix}{state["day"]}.json', text)
    atomic_write_text(root / f"{prefix}current.json", text)
    return state


def _allowed(now) -> bool:
    return (
        settings.service_role in {"standalone", "platform"}
        and preferences.get_data_sync_auto_enabled()
        and trading_day.is_trading_day(now) is True
    )


def request_sync(repo, capset, quote_service=None) -> dict | None:
    """固定触发点登记当天请求; 重启后仅续跑已经登记的等待请求。"""
    now = cn_now()
    if not _allowed(now) or now.time().replace(tzinfo=None) < SYNC_TIME:
        return None
    if not _dispatch_lock.acquire(blocking=False):
        return None
    try:
        state = _load(repo)
        if state and state["day"] == now.date().isoformat():
            return _dispatch(repo, capset, quote_service, state)
        if state and state["status"] in {"pending", "running"}:
            _save(repo, {**state, "status": "interrupted", "error": "等待请求已跨日, 未补齐原交易日"})
        state = _save(repo, {"day": now.date().isoformat(), "status": "pending"})
        return _dispatch(repo, capset, quote_service, state)
    finally:
        _dispatch_lock.release()


def retry_pending(repo, capset, quote_service=None) -> dict | None:
    """Recover a missed close trigger or a failed daily finalization, never cancellation."""
    if not _dispatch_lock.acquire(blocking=False):
        return None
    try:
        state = _load(repo)
        now = cn_now()
        if ((state is None or state["day"] != str(now.date()))
                and _allowed(now) and now.time().replace(tzinfo=None) >= SYNC_TIME):
            if state and state["status"] in {"pending", "running"}:
                _save(repo, {**state, "status": "interrupted", "error": "等待请求已跨日, 未补齐原交易日"})
            state = _save(repo, {"day": now.date().isoformat(), "status": "pending"})
        if (state is not None and state["status"] == "failed"
                and state.get("daily_settlement_status") == "failed"
                and state["day"] == str(now.date()) and _allowed(now)
                and now >= datetime.fromisoformat(state["daily_settlement_retry_at"])):
            state = _save(repo, {**state, "status": "pending"})
        if state is None or state["status"] not in {"pending", "running"}:
            return state
        return _dispatch(repo, capset, quote_service, state)
    finally:
        _dispatch_lock.release()


def request_startup(repo, capset, quote_service=None, *, restart=True, stop=None) -> dict | None:
    """显式启动重新检查当天缺口; 后台等待只续跑本次启动请求。"""
    now = cn_now()
    if not _allowed(now) or (stop is not None and stop.is_set()):
        return None
    if not _dispatch_lock.acquire(blocking=False):
        return {"day": now.date().isoformat(), "status": "pending", "trigger": "startup"}
    try:
        state = _load(repo, trigger="startup")
        if not restart and state is not None:
            return _dispatch(repo, capset, quote_service, state, stop=stop)
        if state and state["status"] == "running":
            state = _dispatch(repo, capset, quote_service, state, stop=stop)
            if state["status"] == "running":
                return state
        state = _save(repo, {"day": now.date().isoformat(), "status": "pending", "trigger": "startup"})
        return _dispatch(repo, capset, quote_service, state, stop=stop)
    finally:
        _dispatch_lock.release()


def _dispatch(repo, capset, quote_service, state: dict, *, stop=None) -> dict:
    now = cn_now()
    if state["day"] != now.date().isoformat():
        return _save(repo, {**state, "status": "interrupted", "error": "等待请求已跨日, 未补齐原交易日"})
    if not _allowed(now) or (
        state.get("trigger") != "startup" and now.time().replace(tzinfo=None) < SYNC_TIME
    ):
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
    owner = f"post_close_sync:{uuid.uuid4().hex}"
    if not pipeline_jobs.try_acquire_run_slot(owner):
        return state
    try:
        job_id, is_new = store.create(
            kind="daily_pipeline", source=state.get("trigger", "scheduled"), long_running=True,
            request={"day": state["day"], "periods": list(PERIODS), "mode": "missing"},
        )
        if not is_new:
            return state
        try:
            state = _save(repo, {**state, "status": "running", "job_id": job_id})
        except Exception:
            store.fail(job_id, "盘后补缺请求状态保存失败, 尚未开始补缺")
            raise

        def check_cancelled():
            if pipeline_jobs.is_cancelled(job_id) or (stop is not None and stop.is_set()):
                raise pipeline_jobs.JobCancelledError(job_id)

        def run():
            target = date.fromisoformat(state["day"])
            results = {}
            incomplete = []
            for index, period in enumerate(PERIODS):
                check_cancelled()

                def progress(stage, pct, msg, stage_pct=None, skip_log=False, *, offset=index, label=period):
                    check_cancelled()
                    store.progress(
                        job_id, stage, min(99, (offset * 100 + pct) // len(PERIODS)),
                        f"{label}: {msg}", stage_pct=stage_pct, skip_log=skip_log,
                    )

                try:
                    result = run_repair_daily(
                        repo, capset, target, target, on_progress=progress,
                        quote_service=quote_service, period=period, mode="missing",
                    )
                except pipeline_jobs.JobCancelledError:
                    raise
                except Exception as exc:
                    logger.exception("盘后%s补缺失败: %s", period, state["day"])
                    result = {"error": str(exc)}
                results[period] = result
                # Preserve partial diagnostics even if no public release is possible.
                state["periods"] = results
                _save(repo, state)
                if (
                    result.get("error") or result.get("remaining")
                    or result.get("gap_check_warning") or result.get("warning")
                ):
                    incomplete.append(f"{period}: {result}")
            check_cancelled()
            release = None
            settled_at = cn_now()
            if settled_at.date() == target and settled_at.time().replace(tzinfo=None) >= SYNC_TIME:
                attempts = state.get("daily_settlement_attempts", 0) + 1
                state.update(daily_settlement_status="running", daily_settlement_attempts=attempts)
                _save(repo, state)
                try:
                    release = finalize_daily_settlement(repo, capset, target, check_cancelled)
                except pipeline_jobs.JobCancelledError:
                    raise
                except Exception:
                    state.update(
                        daily_settlement_status="failed",
                        daily_settlement_retry_at=(cn_now() + timedelta(
                            seconds=min(300, 30 * 2 ** min(attempts, 4)),
                        )).isoformat(),
                    )
                    _save(repo, state)
                    raise
                state["daily_settlement_status"] = "succeeded"
                state["daily_settlement_release_id"] = release["release_id"] if release else None
                _save(repo, state)
            if incomplete:
                raise RuntimeError("盘后补缺未完成: " + "; ".join(incomplete))
            check_cancelled()
            release = release or publish_data_release(
                repo.store.data_dir, reason="post_close_sync",
                write_lock=getattr(repo, "_write_lock", None),
            )
            return {"day": state["day"], "periods": results, "data_release_id": release["release_id"]}

        try:
            result = pipeline_jobs.run_with_capacity(job_id, run)
            store.succeed(job_id, result)
        except pipeline_jobs.JobCancelledError:
            store.cancelled(job_id)
        except Exception as exc:
            logger.exception("盘后补缺失败: %s", state["day"])
            store.fail(job_id, str(exc))
        job = store.get(job_id)
        return _save(repo, {**state, "status": job["status"], "error": job.get("error")})
    finally:
        pipeline_jobs.release_run_slot(owner)
