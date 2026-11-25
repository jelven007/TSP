"""预选股分钟采集的单进程监督器; 金融公式、Provider和公共发布仍走原边界。"""
from __future__ import annotations

import contextlib
import multiprocessing
import os
import threading
import time
from datetime import date
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 180.0


def _collect(job: dict, *, load_sources: bool = True) -> dict:
    """子进程按目标日/预选成员取数落盘; 序号在网络调用前分配, 发布留给父进程。

    逐批记录实际覆盖和错误; 空数据、能力或源错误不能冒充采集成功。
    有有效子集即可落盘并报告missing_symbols计数; 策略阶段仍须独立校验
    启动价格和MA窗口。子进程不会将落盘成功当作公共发布成功。
    这里的 completed_at 是本批采集结束时钟, 不能证明每只股票均有启动行,
    也不能当作普通全市场分钟采集完成证据。
    """
    import polars as pl

    from app.config import settings
    from app.services import kline_sync
    from app.services.minute_write_order import begin_minute_request
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

    # spawn继承受控环境; 只显式传数据路径, 不通过IPC传密码或Provider对象。
    settings.data_dir = Path(job["data_dir"])
    minute_dir = settings.data_dir / "kline_minute"
    result = {
        "pid": os.getpid(), "day": job["day"], "provider": job["provider"],
        "mode": job["mode"], "requested_symbols": len(job["symbols"]),
        "covered_symbols": 0, "written_rows": 0, "requests": 0,
        "request_seq": None, "error": None,
    }
    started = time.perf_counter()
    try:
        result["request_seq"] = begin_minute_request(minute_dir)
        if job["provider"] == "tickflow":
            caps = CapabilitySet({
                Cap(name): CapabilityLimits(**limits)
                for name, limits in job["capabilities"].items()
            })
            if not caps.has(Cap.INTRADAY_BATCH):
                raise ValueError("selected minute batch capability unavailable")
            frame, requests = kline_sync.fetch_intraday_full_market_burst(
                job["symbols"], caps, count=300 if job["mode"] == "full" else 3,
            )
        else:
            from app.data_providers import custom as custom_sources
            # spawn不会继承主进程注册表, 先从同一受控目录加载源及内置插件。
            if load_sources:
                custom_sources.load_all()
            provider, _, error = kline_sync._resolve_minute_provider(job["provider"])
            if error or provider is None:
                raise ValueError(error or "selected minute provider unavailable")
            fetched = (
                kline_sync.fetch_intraday_custom_latest(
                    provider, job["provider"], symbols=job["symbols"])
                if job["mode"] == "increment" else None
            )
            frame, requests = (
                fetched if fetched is not None else
                kline_sync.fetch_intraday_custom_batch(provider, job["provider"], job["symbols"])
            )
        result["requests"] = requests
        result["fetch_ms"] = (time.perf_counter() - started) * 1000
        if not frame.is_empty():
            frame = frame.filter(
                pl.col("symbol").is_in(job["symbols"])
                & (pl.col("datetime").dt.date() == date.fromisoformat(job["day"]))
            )
        if frame.is_empty():
            raise ValueError("no current-day selected minute data")
        result["covered_symbols"] = frame["symbol"].n_unique()
        write_started = time.perf_counter()
        result["written_rows"] = kline_sync._write_minute_partition(
            frame, minute_dir, request_seq=result["request_seq"],
            isolate_suspension_conflicts=True,
        )
        result["write_ms"] = (time.perf_counter() - write_started) * 1000
    except Exception as exc:
        # 网络、源装载和写入异常转实际回执, 父进程据此决定重试而不发布成功完成。
        result["requests"] = getattr(exc, "requests", result["requests"])
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["missing_symbols"] = result["requested_symbols"] - result["covered_symbols"]
    result["completed_at"] = time.time()
    return result


def _worker(connection) -> None:
    """一个子进程反复接收单批任务; 父进程停止或断开即退出。"""
    sources_loaded = False
    try:
        while True:
            try:
                job = connection.recv()
            except EOFError:
                return
            connection.send(_collect(job, load_sources=not sources_loaded))
            sources_loaded |= job["provider"] != "tickflow"
    finally:
        connection.close()


def _key(job: dict) -> tuple:
    # 相同成员的心跳/序号更新不能取消正在采集的同一批。
    return (job["data_dir"], job["day"], job["provider"], tuple(job["symbols"]),
            repr(job["capabilities"]), job.get("source_generation"))


class SelectedMinuteProcess:
    """每个平台一个spawn子进程、一个在途批次, 无待执行任务队列。"""

    def __init__(self, *, worker_target=_worker,
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._context = multiprocessing.get_context("spawn")
        self._target = worker_target
        self._timeout = timeout_seconds
        self._lock = threading.RLock()
        self._process = None
        self._connection = None
        self._job = None
        self._identity = None
        self._started_mono = None
        self._state = {"rounds": 0, "last_error": None}

    def _dispose(self) -> None:
        """阻塞取数可终止, 回收有界; 不会等待Provider自行返回。"""
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(1)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(1)
            self._process.close()
        if self._connection is not None:
            self._connection.close()
        self._process = self._connection = self._job = self._started_mono = self._identity = None

    def _failure(self, error: str) -> dict:
        reply = {
            "pid": self._process.pid if self._process else None,
            "requested_symbols": len(self._job["symbols"]) if self._job else 0,
            "covered_symbols": 0, "written_rows": 0, "requests": 0,
            "completed_at": time.time(), "error": error,
        }
        reply["missing_symbols"] = reply["requested_symbols"]
        return reply

    def _record(self, reply: dict) -> dict:
        """保存实际批次回执并清除在途任务; 成功复用空闲进程, 失败回收由调用方负责。"""
        self._state.update(reply)
        self._state["last_error"] = reply["error"]
        self._state["rounds"] += 1
        self._job = self._started_mono = None
        return reply

    def advance(self, job: dict | None) -> dict | None:
        """小步监督一个在途批次; 成员/源变化或取消回收进程, 超时/退出保存失败。

        同身份心跳不打断取数; 没有第二批待执行队列, 失败后由后续监督轮次重启。
        """
        with self._lock:
            if job is None:
                if self._job is not None:
                    self._state["last_error"] = "selected minute request cancelled"
                self._dispose()
                return None
            if self._identity is not None and _key(job) != self._identity:
                self._state["last_error"] = "selected minute members or source changed"
                self._dispose()
            if self._job is not None:
                if time.monotonic() - self._started_mono >= self._timeout:
                    reply = self._failure(f"selected minute request timed out after {self._timeout:g}s")
                    self._dispose()
                    return self._record(reply)
                if self._connection.poll():
                    try:
                        return self._record(self._connection.recv())
                    except (EOFError, OSError):
                        pass
                if not self._process.is_alive():
                    reply = self._failure(f"selected minute process exited ({self._process.exitcode})")
                    self._dispose()
                    return self._record(reply)
                return None
            try:
                if self._process is not None and not self._process.is_alive():
                    self._dispose()
                if self._process is None:
                    parent, child = self._context.Pipe()
                    self._connection = parent
                    self._process = self._context.Process(
                        target=self._target, args=(child,), name="selected-minute", daemon=True,
                    )
                    try:
                        self._process.start()
                    finally:
                        child.close()
                self._job = dict(job)
                self._identity = _key(job)
                self._started_mono = time.monotonic()
                self._state["started_at"] = time.time()
                self._connection.send(job)
            except (OSError, RuntimeError) as exc:
                reply = self._failure(f"selected minute process start failed: {type(exc).__name__}")
                with contextlib.suppress(ValueError, AssertionError):
                    self._dispose()
                return self._record(reply)
            return None

    def status(self) -> dict:
        """只返回进程和覆盖计数; 不将租户预选代码发布到公共状态。"""
        with self._lock:
            return {
                **self._state, "pid": self._process.pid if self._process else None,
                "running": self._process is not None and self._process.is_alive(),
                "inflight": self._job is not None, "timeout_seconds": self._timeout,
            }

    def close(self) -> None:
        """停止并回收所属子进程和 IPC; 不触碰已经落盘的分钟分区。"""
        with self._lock:
            self._dispose()
