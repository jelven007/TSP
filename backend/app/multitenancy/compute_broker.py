"""Host-only Unix-socket broker for one disposable container per compute job."""
# ruff: noqa: RUF001 -- Chinese compute messages use localized punctuation.
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import selectors
import signal
import socketserver
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import suppress
from pathlib import Path

from app.multitenancy.executor import KINDS, MAX_MESSAGE_BYTES
from app.services.strategy_resources import (
    BudgetPermits,
    ConcurrencyConfig,
    budget_status,
    host_resources,
)

STDERR_TAIL_BYTES = 8 * 1024
logger = logging.getLogger(__name__)


def append_stderr_tail(tail: bytearray, block: bytes) -> None:
    tail.extend(block)
    if len(tail) > STDERR_TAIL_BYTES:
        del tail[:-STDERR_TAIL_BYTES]


class ComputeBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False

    def __init__(self, socket_path: Path, image: str, market_root: Path, capacity: int,
                 *, memory_mb: int = 4096, cpus: float = 2, queue_seconds: float = 2,
                 health_cache_seconds: float = 60, config_file: Path | None = None,
                 host_memory_mb: int | None = None, host_cpus: float | None = None):
        if (not 1 <= capacity <= 16 or not 256 <= memory_mb <= 65536
                or not math.isfinite(cpus) or not .25 <= cpus <= 16
                or not math.isfinite(queue_seconds) or not 0 <= queue_seconds <= 30
                or not math.isfinite(health_cache_seconds) or not 1 <= health_cache_seconds <= 300):
            raise ValueError("invalid compute resource budget")
        self.image = image
        self.market_root = market_root
        self.memory = "4g" if memory_mb == 4096 else f"{memory_mb}m"
        self.cpus = f"{cpus:g}"
        self.task_memory_mb, self.task_cpus = memory_mb, cpus
        detected_memory, detected_cpus = host_resources()
        self.host_memory_mb = detected_memory if host_memory_mb is None else host_memory_mb
        self.host_cpus = detected_cpus if host_cpus is None else host_cpus
        self.capacity = capacity
        self.config = ConcurrencyConfig(config_file) if config_file is not None else None
        self.queue_seconds = queue_seconds
        self.health_cache_seconds = health_cache_seconds
        self.permits = BudgetPermits(self._resource_policy)
        self.waiters = threading.BoundedSemaphore(16)
        self.tenant_lock = threading.Lock()
        self.active_tenants: set[str] = set()
        self._tenant_counts: dict[str, int] = {}
        self.health_lock = threading.Lock()
        self.health_cache: tuple[float, bool] | None = None
        self.stopped = threading.Event()
        super().__init__(str(socket_path), Handler)

    def _resource_policy(self) -> dict:
        configured = self.config.read() if self.config is not None else self.capacity
        return budget_status(
            self.host_memory_mb, self.host_cpus, configured,
            task_memory_mb=self.task_memory_mb, task_cpus=self.task_cpus,
            ceiling=self.capacity,
        )

    def resource_status(self) -> dict:
        return {**self._resource_policy(), **self.permits.usage(),
                "config_enabled": self.config is not None, "execution_mode": "sandbox"}

    def acquire_tenant(self, tenant: str, *, administrator: bool) -> bool:
        with self.tenant_lock:
            if tenant in self.active_tenants and not administrator:
                return False
            self._tenant_counts[tenant] = self._tenant_counts.get(tenant, 0) + 1
            self.active_tenants.add(tenant)
            return True

    def release_tenant(self, tenant: str) -> None:
        with self.tenant_lock:
            remaining = self._tenant_counts.get(tenant, 1) - 1
            if remaining:
                self._tenant_counts[tenant] = remaining
            else:
                self._tenant_counts.pop(tenant, None)
                self.active_tenants.discard(tenant)

    def command(self, name: str, market_path: Path | None = None) -> list[str]:
        allocation = self.permits.allocation()
        if allocation is None:
            policy = self._resource_policy()
            memory_mb, cpus = policy["task_memory_mb"], policy["task_cpus"]
        else:
            memory_mb, cpus = allocation
        memory = "4g" if memory_mb == 4096 else f"{memory_mb}m"
        # No host credentials, private files, Docker socket, network, or writable
        # shared mounts enter this container. env -i also removes image defaults.
        return [
            "docker", "run", "--rm", "--name", name, "-i", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "10001:10001", "--pids-limit", "128", "--memory", memory,
            "--memory-swap", memory, "--cpus", f"{cpus:.6f}".rstrip("0").rstrip("."),
            "--ulimit", "nofile=256:256",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,uid=10001,gid=10001",
            "--mount", f"type=bind,src={market_path or self.market_root},dst=/market,readonly",
            "--entrypoint", "/usr/bin/env", self.image, "-i",
            "PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONPATH=/app",
            "PYTHONDONTWRITEBYTECODE=1", "HOME=/tmp", "XDG_CACHE_HOME=/tmp/xdg",
            "DATA_DIR=/market", "TICKFLOW_ENV_FILE=/dev/null",
            "TIERS_YAML=/app/tiers.yaml", "TZ=Asia/Shanghai",
            f"POLARS_MAX_THREADS={max(1, math.floor(cpus))}",
            f"NUMBA_NUM_THREADS={max(1, math.floor(cpus))}",
            "OPENBLAS_NUM_THREADS=1", "OMP_NUM_THREADS=1",
            "NUMBA_CACHE_DIR=/tmp/numba", "/app/.venv/bin/python", "-m",
            "app.multitenancy.compute_worker",
        ]

    def health(self, market_path: Path) -> dict:
        now = time.monotonic()
        cached = self.health_cache
        cache_age = now - cached[0] if cached is not None else None
        cache_ttl = self.health_cache_seconds if cached and cached[1] else 5
        if cached is not None and cache_age <= cache_ttl:
            return {
                "ready": cached[1], "cached": True,
                "age_seconds": round(cache_age, 3),
            }
        with self.health_lock:
            now = time.monotonic()
            cached = self.health_cache
            cache_age = now - cached[0] if cached is not None else None
            cache_ttl = self.health_cache_seconds if cached and cached[1] else 5
            if cached is not None and cache_age <= cache_ttl:
                return {
                    "ready": cached[1], "cached": True,
                    "age_seconds": round(cache_age, 3),
                }
            if not self.permits.acquire(blocking=False):
                ready = bool(cached and cached[1] and now - cached[0] <= 300)
                return {
                    "ready": ready, "cached": cached is not None,
                    "age_seconds": round(now - cached[0], 3) if cached else None,
                }
            name = "tsp-compute-health-" + uuid.uuid4().hex
            ready = False
            try:
                result = subprocess.run(
                    self.command(name, market_path),
                    input=b'{"kind":"health"}\n',
                    capture_output=True,
                    timeout=20,
                    check=False,
                )
                try:
                    message = json.loads(result.stdout.splitlines()[-1])
                except (IndexError, json.JSONDecodeError):
                    message = {}
                ready = bool(
                    result.returncode == 0
                    and message.get("type") == "result"
                    and message.get("payload", {}).get("ready") is True
                )
                if not ready:
                    tail = result.stderr[-STDERR_TAIL_BYTES:].decode(errors="replace")
                    logger.warning(
                        "compute health sandbox failed: returncode=%s stderr=%r",
                        result.returncode, tail,
                    )
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("compute health sandbox failed: %s", type(exc).__name__)
            finally:
                if not ready:
                    with suppress(OSError, subprocess.SubprocessError):
                        subprocess.run(
                            ["docker", "rm", "-f", name],
                            capture_output=True, timeout=15, check=False,
                        )
                self.permits.release()
            self.health_cache = (time.monotonic(), ready)
            return {"ready": ready, "cached": False, "age_seconds": 0.0}


class Handler(socketserver.StreamRequestHandler):
    def send(self, message):
        self.wfile.write(json.dumps(message, ensure_ascii=False, allow_nan=False).encode() + b"\n")
        self.wfile.flush()

    def handle(self):
        broker = self.server
        tenant = None
        name = "tsp-compute-" + uuid.uuid4().hex
        process = None
        acquired = False
        terminal_message = None
        stderr_tail = bytearray()
        try:
            self.connection.settimeout(30)
            line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                raise ValueError("计算任务输入过大或不完整")
            payload = json.loads(line)
            if payload.get("kind") == "resources":
                self.send({"type": "result", "payload": broker.resource_status()})
                return
            from app.multitenancy.publication import release_path

            market_path = release_path(broker.market_root, payload.get("public_release_id"))
            if payload.get("kind") == "health":
                self.send({"type": "result", "payload": broker.health(market_path)})
                return
            tenant = payload.get("tenant_id")
            if (payload.get("kind") not in KINDS or not isinstance(tenant, str)
                    or not 1 <= len(tenant) <= 128):
                raise ValueError("计算任务格式错误")
            if not broker.waiters.acquire(blocking=False):
                self.send({"type": "error", "status": 429, "message": "计算任务繁忙，请稍后重试"})
                return
            try:
                acquired = broker.permits.acquire(timeout=broker.queue_seconds)
            finally:
                broker.waiters.release()
            if not acquired:
                self.send({"type": "error", "status": 429, "message": "计算任务繁忙，请稍后重试"})
                return
            if not broker.acquire_tenant(tenant, administrator=payload.get("administrator") is True):
                self.send({"type": "error", "status": 429, "message": "当前租户已有计算任务，请稍后重试"})
                tenant = None
                return
            with tempfile.TemporaryFile() as task, selectors.DefaultSelector() as events:
                task.write(line)
                task.seek(0)
                process = subprocess.Popen(
                    broker.command(name, market_path), stdin=task, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                events.register(process.stdout, selectors.EVENT_READ, "stdout")
                events.register(process.stderr, selectors.EVENT_READ, "stderr")
                events.register(self.connection, selectors.EVENT_READ, "client")
                deadline = time.monotonic() + (60 if payload["kind"] in {"describe", "plan"} else 7200)
                buffer = bytearray()
                output_bytes = 0
                terminal = False
                last_heartbeat = time.monotonic()
                while events.get_map():
                    if broker.stopped.is_set():
                        raise ValueError("计算服务正在重启")
                    if time.monotonic() >= deadline:
                        raise ValueError("计算任务超时")
                    if time.monotonic() - last_heartbeat >= 10:
                        self.send({"type": "heartbeat"})
                        last_heartbeat = time.monotonic()
                    for key, _ in events.select(timeout=0.5):
                        if key.data == "client":
                            if not self.connection.recv(1):
                                return
                            raise ValueError("计算请求不允许追加输入")
                        block = os.read(key.fd, 64 * 1024)
                        if not block:
                            events.unregister(key.fileobj)
                            continue
                        output_bytes += len(block)
                        if output_bytes > MAX_MESSAGE_BYTES:
                            raise ValueError("计算任务输出超过限制")
                        if key.data == "stderr":
                            append_stderr_tail(stderr_tail, block)
                            continue
                        buffer.extend(block)
                        while b"\n" in buffer:
                            message_line, _, remaining = buffer.partition(b"\n")
                            buffer = bytearray(remaining)
                            message = json.loads(message_line)
                            if message.get("type") not in {"progress", "result", "error"}:
                                raise ValueError("计算输出格式错误")
                            if message.get("type") in {"result", "error"}:
                                terminal = True
                                terminal_message = message
                            else:
                                self.send(message)
                    if process.poll() is not None and len(events.get_map()) == 1:
                        break
                if not terminal:
                    raise ValueError("计算进程退出时未返回结果")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            terminal_message = {"type": "error", "status": 503 if broker.stopped.is_set() else 400, "message": str(exc)}
        finally:
            if process is not None:
                with suppress(subprocess.SubprocessError, OSError):
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
                if process.poll() is None:
                    process.kill()
                with suppress(subprocess.SubprocessError):
                    _stdout, remaining_stderr = process.communicate(timeout=5)
                    append_stderr_tail(stderr_tail, remaining_stderr)
            if tenant is not None and acquired:
                broker.release_tenant(tenant)
            if acquired:
                broker.permits.release()
            # The next task may start as soon as the client receives a terminal
            # event. Release both permits after cleanup, before sending it.
            if terminal_message is not None:
                if terminal_message.get("type") == "error" and stderr_tail:
                    logger.warning(
                        "compute job %s stderr tail: %s",
                        name, stderr_tail.decode(errors="replace"),
                    )
                with suppress(OSError):
                    self.send(terminal_message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--market-root", type=Path, required=True)
    parser.add_argument("--capacity", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument("--cpus", type=float, default=2)
    parser.add_argument(
        "--config-file", type=Path,
        default=os.environ.get("TSP_COMPUTE_CONFIG_FILE"),
        help="Read-only host path to the platform preferences.json",
    )
    parser.add_argument(
        "--queue-seconds", type=float,
        default=float(os.environ.get("TSP_COMPUTE_QUEUE_SECONDS", "2")),
    )
    args = parser.parse_args()
    market = args.market_root.resolve(strict=True)
    if not market.is_dir() or not 1 <= args.capacity <= 16:
        raise ValueError("invalid compute broker configuration")
    if (market / "user_data").exists() or (market / ".env").exists():
        raise ValueError("compute market mount must not contain private state or credentials")
    args.socket.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    # A stale socket is removed by the service manager, never blindly unlink an
    # active broker's socket at startup.
    with ComputeBroker(args.socket, args.image, market, args.capacity,
                       memory_mb=args.memory_mb, cpus=args.cpus,
                       queue_seconds=args.queue_seconds, config_file=args.config_file) as server:
        os.chmod(args.socket, 0o660)
        def shutdown(*_):
            server.stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, shutdown)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
