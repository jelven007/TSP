"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging

from fastapi import APIRouter, HTTPException, Request

from app.jobs import daily_pipeline
from app.services.pipeline_jobs import (
    JobCancelledError,
    job_store,
    release_run_slot,
    run_with_capacity,
    try_acquire_run_slot,
)
from app.api.data import invalidate_storage_cache

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")
_background_tasks: dict[str, asyncio.Task] = {}

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。"""
    return await trigger_pipeline_job(
        request.app.state.repo,
        request.app.state.capabilities,
        getattr(request.app.state, "quote_service", None),
    )


async def trigger_pipeline_job(repo, capset, quote_service=None, *, source="manual") -> dict:
    """触发盘后管道后台任务(HTTP 端点与 AI 助手共用同一条触发路径)。

    若已有任务在跑,**返回该任务 id 而不是开新任务**(防止并发拉数据撞限流)。
    卡死判定按「进度停滞」而非总时长(慢带宽下长任务不会被误杀), 见 reap_stale。
    """
    # 检测卡死的 running job (如 reload 后孤儿 task / 网络读无限阻塞)。
    # reap_stale 会在 /run 和 /jobs/{id} 轮询端点都调用,保证卡死后能自愈。
    job_store.reap_stale()

    # 单飞: 复用任何活跃任务, is_new=False 时不再调度新任务
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

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        # 重任务执行槽: 防僵尸并发(reap 后线程仍活时新任务不得并行写 parquet)
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = quote_service
        try:
            loop = asyncio.get_event_loop()

            def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                         skip_log: bool = False) -> None:
                job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

            def _run() -> dict:
                try:
                    run = initial_sync.run_initial_sync if initialize else daily_pipeline.run_now
                    if qs:
                        with qs.paused():
                            return run(repo, capset, on_progress=progress)
                    return run(repo, capset, on_progress=progress)
                finally:
                    repo.refresh_cache()

            result = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
        except JobCancelledError:
            job_store.cancelled(job_id)
            logger.warning("pipeline job %s cancelled", job_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("pipeline failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

    background = asyncio.create_task(task())
    _background_tasks[job_id] = background
    background.add_done_callback(lambda _: _background_tasks.pop(job_id, None))
    return {"job_id": job_id, "reused": False}


async def stop_initial_sync_job(job_id: str | None) -> None:
    """Drain the startup worker before its repository and providers are closed."""
    background = _background_tasks.get(job_id) if job_id else None
    if background is not None:
        job_store.terminate(job_id, "服务关闭,首次同步将在下次启动重试")
        # Cancelling the asyncio wrapper would leave its executor thread writing.
        await asyncio.shield(background)


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    # 每次轮询都检查卡死 job — 前端持续轮询, 进度停滞超阈值后必定自愈,
    # 无需用户再次手动点「同步」。
    job_store.reap_stale()
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """手动取消活跃任务；终态在执行体实际退出后发布。"""
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if j["status"] not in ("queued", "running", "cancelling"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    job_store.terminate(job_id, "用户手动取消")
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict:
    return {
        "active_id": job_store.active_id(),
        "jobs": job_store.list_recent(limit=limit),
    }
