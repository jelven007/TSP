"""Client for the local, bounded compute broker.

The API gets a Unix socket, never a Docker socket. The host broker controls the
image, mounts, limits and entrypoint; the request cannot override any of them.
"""
# ruff: noqa: RUF001 -- Chinese compute messages use localized punctuation.
from __future__ import annotations

import json
import math
import os
import socket
import time
from collections.abc import Callable

from app.cell.context import AccessError

MAX_MESSAGE_BYTES = 128 * 1024 * 1024
KINDS = frozenset({"describe", "plan", "strategy", "backtest", "optimize", "walkforward", "mining",
                   "signal_backtest", "factor_backtest", "factor_batch", "auto_screen", "monitor",
                   "daily_stock_pools", "daily_selections", "monitor_breadth"})


def monitor_timeout(payload):
    """单请求排队/计算最多 60 秒, 再取动作截止和派发绝对期限的剩余最小值。

    无动作 joined 请求未携带派发期限时使用默认预算; 返回清理时间另计,
    本函数的预算不保证整轮汇合或业务提交在 60 秒内完成。
    """
    from datetime import datetime

    from app.market_time import cn_now

    value = float(payload.get("phase_timeout_seconds", 60))
    if not math.isfinite(value) or value <= 0:
        raise ValueError("invalid monitor phase timeout")
    limit = min(60., value)
    deadlines = [datetime.fromisoformat(action["retry_until"])
                 for plan in payload.get("strategy_task_plans", {}).values()
                 for action in plan.get("actions", [])
                 if action.get("retry_until") and action["phase"] != "expire"]
    if payload.get("phase_deadline"):
        deadlines.append(datetime.fromisoformat(payload["phase_deadline"]))
    if deadlines:
        from app.market_time import as_cn_datetime
        limit = min(limit, max(0., (min(as_cn_datetime(value) for value in deadlines) - cn_now()).total_seconds()))
    return limit


class ComputeExecutor:
    """隔离计算客户端; isolated只选择编排方式, 不保证资源足够让两侧同时运行。"""

    isolated = True
    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or os.environ.get("TSP_COMPUTE_SOCKET", "/run/tsp/compute.sock")

    def health(self):
        return self.execute({"kind": "health"})

    def resources(self):
        return self.execute({"kind": "resources"})

    def execute(
        self, payload: dict, progress: Callable[[dict], None] | None = None, cancel=None,
    ) -> dict:
        """经 Unix socket 提交有界 JSON, 请求不能选择镜像、挂载或资源上限。

        监控响应等待额外留出 25 秒回收时间, 不延长计算或新买入窗口。取消关闭
        连接, Broker 通过 EOF 回收容器; 错误保留本次真实计算耗时。
        """
        if payload.get("kind") not in KINDS and payload.get("kind") not in {"health", "resources"}:
            raise ValueError("unsupported compute operation")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode() + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise AccessError(413, "计算任务输入过大")
        if not hasattr(socket, "AF_UNIX"):
            raise AccessError(503, "当前系统缺少隔离计算服务所需的本地通信支持")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(self.socket_path)
                client.settimeout(30)
                client.sendall(encoded)
                client.settimeout(1)
                pending = bytearray()
                deadline = time.monotonic() + (
                    25 if payload["kind"] == "health" else 10 if payload["kind"] == "resources"
                    else monitor_timeout(payload) + 25 if payload["kind"] in {"monitor", "monitor_breadth"}
                    else 300 if payload["kind"] == "daily_selections" else 7500
                )
                while True:
                    if time.monotonic() > deadline:
                        raise AccessError(503, "计算服务响应超时")
                    if cancel is not None and cancel.is_set():
                        # 退出 socket 形成 EOF; Broker 先回收容器及其子进程,
                        # 再释放租户和计算槽, 避免下个任务占用未回收资源。
                        raise AccessError(409, "计算任务已取消")
                    try:
                        block = client.recv(64 * 1024)
                    except TimeoutError:
                        continue
                    if not block:
                        raise AccessError(503, "计算服务连接中断")
                    pending.extend(block)
                    if len(pending) > MAX_MESSAGE_BYTES:
                        raise AccessError(413, "计算任务输出过大")
                    while b"\n" in pending:
                        line, _, remaining = pending.partition(b"\n")
                        pending = bytearray(remaining)
                        message = json.loads(line)
                        if message.get("type") == "progress":
                            if progress is not None:
                                progress(message["payload"])
                        elif message.get("type") == "result":
                            return message["payload"]
                        elif message.get("type") == "error":
                            error = AccessError(int(message.get("status", 400)), message["message"])
                            error.compute_timing = message.get("_compute_timing") or {}
                            raise error
        except (FileNotFoundError, ConnectionRefusedError, PermissionError, TimeoutError) as exc:
            raise AccessError(503, "计算服务暂不可用，请稍后重试") from exc
