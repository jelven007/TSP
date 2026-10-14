"""Client for the local, bounded compute broker.

The API gets a Unix socket, never a Docker socket. The host broker controls the
image, mounts, limits and entrypoint; the request cannot override any of them.
"""
# ruff: noqa: RUF001 -- Chinese compute messages use localized punctuation.
from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Callable

from app.cell.context import AccessError

MAX_MESSAGE_BYTES = 128 * 1024 * 1024
KINDS = frozenset({"describe", "plan", "strategy", "backtest", "optimize", "walkforward", "mining",
                   "signal_backtest", "factor_backtest", "factor_batch", "auto_screen", "monitor"})


class ComputeExecutor:
    def __init__(self, socket_path: str | None = None):
        self.socket_path = socket_path or os.environ.get("TSP_COMPUTE_SOCKET", "/run/tsp/compute.sock")

    def health(self):
        return self.execute({"kind": "health"})

    def execute(
        self, payload: dict, progress: Callable[[dict], None] | None = None, cancel=None,
    ) -> dict:
        if payload.get("kind") not in KINDS and payload.get("kind") != "health":
            raise ValueError("unsupported compute operation")
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str).encode() + b"\n"
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise AccessError(413, "计算任务输入过大")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect(self.socket_path)
                client.settimeout(30)
                client.sendall(encoded)
                client.settimeout(1)
                pending = bytearray()
                deadline = time.monotonic() + (5 if payload["kind"] == "health" else 7500)
                while True:
                    if time.monotonic() > deadline:
                        raise AccessError(503, "计算服务响应超时")
                    if cancel is not None and cancel.is_set():
                        # EOF is cancellation. The broker kills the container,
                        # including descendants, before releasing its permit.
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
                            raise AccessError(int(message.get("status", 400)), message["message"])
        except (FileNotFoundError, ConnectionRefusedError, PermissionError, TimeoutError) as exc:
            raise AccessError(503, "计算服务暂不可用，请稍后重试") from exc
