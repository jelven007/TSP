"""Bounded source-day repair for the latest 20:00 next-session selection window."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from datetime import date, datetime, time, timedelta

import polars as pl

from app.config import settings
from app.market_time import cn_now
from app.services import pipeline_jobs, preferences
from app.services.daily_selections import due_window
from app.services.fs_utils import atomic_write_text
from app.services.post_close_sync import finalize_daily_settlement
from app.services.repair_daily import run_repair_daily

logger = logging.getLogger(__name__)
_lock = threading.Lock()


def input_signature(repo, day):
    """Only target partitions and their dimension evidence; no historical tree scan."""
    root = repo.store.data_dir
    paths = []
    for name in ("kline_daily", "kline_daily_enriched", "kline_minute"):
        paths.extend((root / name / f"date={day}").glob("*.parquet"))
    for name in ("instruments", "instrument_status", "adj_factor"):
        paths.extend((root / name).glob("*.parquet"))
    values = [(str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns)
              for p in sorted(paths)]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _load(repo):
    path = repo.store.data_dir / "selection_daily_repair" / "current.json"
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or not state.get("day") or not state.get("status"):
        raise ValueError("预选日线修复状态无效")
    date.fromisoformat(state["day"])
    return state


def _save(repo, state):
    root = repo.store.data_dir / "selection_daily_repair"
    root.mkdir(parents=True, exist_ok=True)
    state = {**state, "updated_at": cn_now().isoformat()}
    text = json.dumps(state, ensure_ascii=False)
    atomic_write_text(root / f'{state["day"]}.json', text)
    atomic_write_text(root / "current.json", text)
    return state


def repair_inputs(repo, capset, day, *, progress, check_cancelled, quote_service=None,
                  previous=None, diagnostics=None):
    from app.data_providers.suspension_announcements import AnnouncementLookup
    from app.services.selection_suspension_repair import missing_symbols, repair_suspensions

    # One shared request/time budget covers both the initial and residual probe.
    lookup = AnnouncementLookup(check_cancelled=check_cancelled)
    evidence = diagnostics if diagnostics is not None else {}
    offset = 32 * max(0, (previous or {}).get("attempts", 1) - 1)
    try:
        check_cancelled()
        if len(missing_symbols(repo, day)) <= 32:
            progress("verify_suspensions", 5, f"先核验{day}缺日线及已知停牌证券的官方公告")
            evidence["suspension_check"] = repair_suspensions(
                repo, day, check_cancelled=check_cancelled, lookup=lookup, offset=offset)
        else:
            evidence["suspension_check"] = {"added": 0, "diagnostics": {
                "phase": "大范围缺口先补真实日线, 再查证剩余证券"}}
        return _repair_inputs(repo, capset, day, progress=progress, check_cancelled=check_cancelled,
                              quote_service=quote_service, previous=previous,
                              evidence=evidence, lookup=lookup, offset=offset)
    finally:
        lookup.close()


def _repair_inputs(repo, capset, day, *, progress, check_cancelled, quote_service,
                   previous, evidence, lookup, offset):
    from app.services import kline_periods, kline_sync
    from app.services.daily_close_validation import require_final_daily_rows, validate_daily_close
    from app.services.realtime_guard import paused_quotes
    from app.services.selection_suspension_repair import repair_suspensions

    check_cancelled()
    missing = run_repair_daily(
        repo, capset, day, day, period="1d", mode="missing",
        on_progress=progress, quote_service=quote_service,
    )
    evidence["gap_check"] = missing
    check_cancelled()
    if missing.get("remaining"):
        progress("verify_suspensions", 15, f"补数后仍缺{missing['remaining']}行, 再核验停复牌")
        residual = repair_suspensions(repo, day, check_cancelled=check_cancelled,
                                      lookup=lookup, offset=offset)
        evidence["suspension_recheck"] = residual
        if residual["added"]:
            missing = run_repair_daily(
                repo, capset, day, day, period="1d", mode="missing",
                on_progress=progress, quote_service=quote_service)
            evidence["gap_check"] = missing
    if missing.get("error"):
        raise ValueError(str(missing["error"]))
    check_cancelled()
    raw, _ = repo.get_daily_close_inputs(day)
    if raw.is_empty():
        return {**evidence, "day": str(day), "selection_ready": False, "data_release_id": None,
                "gap_check": {**missing, "gap_check_warning": "源交易日暂无日线, 等待补数"}}
    rejected = validate_daily_close(repo, day, isolate_symbols=True) or {}
    if rejected:
        progress("sync_daily", 20, f"主动修复{day}日线/收盘证据异常: {len(rejected)}只")
        # Price disagreement can originate on either side. Refresh official daily
        # rows first, then request only remaining bad auction keys.
        with paused_quotes(quote_service):
            kline_sync.sync_and_persist_daily_batch(
                sorted(rejected), repo, capset,
                start_date=datetime.combine(day, time.min),
                end_date=datetime.combine(day, time.max),
                on_chunk_done=lambda cur, total: progress(
                    "sync_daily", 20 + int(25 * cur / total), f"日线修复 {cur}/{total}"),
                strict=True,
            )
        check_cancelled()
        raw, _ = repo.get_daily_close_inputs(day)
        require_final_daily_rows(raw, day)
        remaining = validate_daily_close(repo, day, isolate_symbols=True) or {}
        if remaining:
            provider, source = kline_periods.require_period_provider("1m", capset)
            stamp = datetime.combine(day, time(15))
            symbols = sorted(remaining)
            for offset in range(0, len(symbols), 100):
                check_cancelled()
                batch = symbols[offset:offset + 100]
                progress("sync_minute", 50, f"{source}: 主动修复15:00收盘分钟 {offset}/{len(symbols)}")
                candidate = kline_periods.fetch_period(
                    provider, "1m", batch, stamp, stamp, minute_adjust="none",
                )
                check_cancelled()
                if candidate.is_empty():
                    continue
                official = raw.select("symbol", pl.col("close").alias("_official_close"))
                counts = candidate.group_by("symbol").len()
                verified = candidate.join(official, on="symbol").join(counts, on="symbol").filter(
                    (pl.col("len") == 1)
                    & ((pl.col("close") - pl.col("_official_close")).abs() <= .005)
                ).select(candidate.columns)
                # A returned price is not permission to invent agreement.
                if not verified.is_empty():
                    with paused_quotes(quote_service):
                        kline_periods.persist_period(repo, "1m", verified)
            kline_periods.refresh_period(repo, "1m")
        check_cancelled()
        validate_daily_close(repo, day)
        repo.refresh_cache()
    if previous and previous.get("result") and previous.get("input_signature") == input_signature(repo, day):
        # Retry unresolved missing symbols without repeatedly republishing an
        # unchanged, already validated release.
        return {**previous["result"], **evidence, "gap_check": missing}
    progress("validate_daily", 80, "重验日线、收盘分钟及加工原价, 发布预选输入")
    release = finalize_daily_settlement(repo, capset, day, check_cancelled)
    return {**evidence, "day": str(day), "gap_check": missing, "repaired_symbols": sorted(rejected),
            "data_release_id": release["release_id"]}


def request_repair(repo, capset, quote_service=None, *, stop=None):
    """One writer per day/input; failed attempts retry before the target open."""
    now = cn_now()
    if (settings.service_role not in {"platform", "standalone"}
            or not preferences.get_data_sync_auto_enabled()
            or (stop is not None and stop.is_set())):
        return None
    window = due_window(now)
    if window is None or not _lock.acquire(blocking=False):
        return None
    try:
        day, target = window
        state = _load(repo)
        if not state or state["day"] != str(day):
            state = _save(repo, {"day": str(day), "as_of": str(target),
                                 "status": "pending", "attempts": 0})
        store = pipeline_jobs.job_store
        if state["status"] == "running":
            job = store.get(state["job_id"])
            status = (job or {}).get("status", "interrupted")
            if status not in {"succeeded", "failed", "cancelled", "interrupted"}:
                return state
            state = _save(repo, {**state, "status": status, "error": (job or {}).get("error")})
        if state["status"] == "cancelled":
            return state
        signature = input_signature(repo, day)
        if (state["status"] == "succeeded" and not state.get("retry_required")
                and state.get("input_signature") == signature):
            return state
        if state.get("next_retry_at") and datetime.fromisoformat(state["next_retry_at"]) > now:
            return state
        owner = f"selection_daily_repair:{uuid.uuid4().hex}"
        if not pipeline_jobs.try_acquire_run_slot(owner):
            return state
        try:
            job_id, created = store.create(
                kind="daily_pipeline", source="scheduled", long_running=True,
                request={"day": str(day), "as_of": str(target), "purpose": "selection_daily_repair"},
            )
            if not created:
                return state
            state = {**state, "status": "running", "job_id": job_id,
                     "attempts": state.get("attempts", 0) + 1, "error": None, "next_retry_at": None}
            try:
                state = _save(repo, state)
            except Exception:
                store.fail(job_id, "预选修复状态保存失败, 尚未开始写入")
                raise

            def check_cancelled():
                if pipeline_jobs.is_cancelled(job_id) or (stop is not None and stop.is_set()):
                    raise pipeline_jobs.JobCancelledError(job_id)
                if due_window(cn_now()) != (day, target):
                    raise ValueError("预选修复窗口已结束, 不发布过期输入")

            def progress(stage, pct, msg, stage_pct=None, skip_log=False):
                check_cancelled()
                store.progress(job_id, stage, min(99, pct), msg,
                               stage_pct=stage_pct, skip_log=skip_log)

            attempt_diagnostics = {}
            try:
                result = pipeline_jobs.run_with_capacity(job_id, lambda: repair_inputs(
                    repo, capset, day, progress=progress, check_cancelled=check_cancelled,
                    quote_service=quote_service,
                    previous=state,
                    diagnostics=attempt_diagnostics,
                ))
                check_cancelled()
                state = {**state, "input_signature": input_signature(repo, day), "result": result}
                retry = bool(result["gap_check"].get("remaining")
                             or result["gap_check"].get("gap_check_warning"))
                warning = ("部分日线仍缺失, 已发布有效日线供预选继续; 后台继续补数"
                           if retry else None)
                if result.get("selection_ready") is False:
                    warning = "源交易日暂无有效日线, 预选等待数据; 后台继续补数"
                state = {**state, "retry_required": retry, "warning": warning}
                store.succeed(job_id, {**result, "warning": warning, "retry_required": retry})
            except pipeline_jobs.JobCancelledError:
                store.cancelled(job_id)
            except Exception as exc:
                logger.exception("次日预选主动修复失败: %s", day)
                store.fail(job_id, str(exc))
            job = store.get(job_id)
            state = {**state, "status": job["status"], "error": job.get("error"),
                     "diagnostics": attempt_diagnostics}
            if state["status"] in {"failed", "interrupted"} or state.get("retry_required"):
                delay = min(300, 60 * 2 ** min(3, state["attempts"] - 1))
                state["next_retry_at"] = (cn_now() + timedelta(seconds=delay)).isoformat()
            return _save(repo, state)
        finally:
            pipeline_jobs.release_run_slot(owner)
    finally:
        _lock.release()
