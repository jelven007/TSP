"""Tracked background pipeline orchestration shared by HTTP and startup."""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from app.jobs import daily_pipeline
from app.services.pipeline_jobs import (
    JobCancelledError,
    job_store,
    release_run_slot,
    run_with_capacity,
    try_acquire_run_slot,
)
from app.services.public_query_cache import invalidate_storage_cache

_long_task_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=2, thread_name_prefix="long-task",
)
_background_tasks: dict[str, asyncio.Task] = {}
logger = logging.getLogger(__name__)


async def trigger_pipeline_job(repo, capset, quote_service=None, *, source="manual") -> dict:
    job_store.reap_stale()
    job_id, is_new = job_store.create(kind="daily_pipeline", source=source)
    if not is_new:
        return {"job_id": job_id, "reused": True}

    from app.services import initial_sync

    try:
        initialize = initial_sync.needs_initial_sync(repo.store.data_dir)
        if initialize:
            initial_sync.mark_pending(repo.store.data_dir)
    except Exception as exc:
        job_store.fail(job_id, f"无法准备首次同步: {exc}")
        return {"job_id": job_id, "reused": False}

    async def task() -> None:
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        try:
            loop = asyncio.get_running_loop()
            if initialize:
                await daily_pipeline.wait_for_catalog_jobs()

            def progress(stage, pct, msg, stage_pct=None, skip_log=False):
                job_store.progress(
                    job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log,
                )

            def run():
                try:
                    execute = initial_sync.run_initial_sync if initialize else daily_pipeline.run_now
                    if quote_service:
                        with quote_service.paused():
                            return execute(repo, capset, on_progress=progress)
                    return execute(repo, capset, on_progress=progress)
                finally:
                    repo.refresh_cache()

            result = await loop.run_in_executor(
                _long_task_executor, run_with_capacity, job_id, run,
            )
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
        except JobCancelledError:
            job_store.cancelled(job_id)
            logger.warning("pipeline job %s cancelled", job_id)
        except Exception as exc:
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(exc))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    background = asyncio.create_task(task())
    _background_tasks[job_id] = background
    background.add_done_callback(lambda _: _background_tasks.pop(job_id, None))
    return {"job_id": job_id, "reused": False}


async def stop_initial_sync_job(job_id: str | None) -> None:
    """Drain the executor before closing its repository and providers."""
    background = _background_tasks.get(job_id) if job_id else None
    if background is not None:
        job_store.terminate(job_id, "服务关闭,首次同步将在下次启动重试")
        # Cancelling the wrapper does not stop its thread from writing.
        await asyncio.shield(background)
