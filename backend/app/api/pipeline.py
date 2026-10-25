"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.services.pipeline_jobs import job_store
from app.services.pipeline_runner import (
    stop_initial_sync_job as stop_initial_sync_job,
    trigger_pipeline_job as trigger_pipeline_job,
)

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。"""
    return await trigger_pipeline_job(
        request.app.state.repo,
        request.app.state.capabilities,
        getattr(request.app.state, "quote_service", None),
    )


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
