"""Host-only Unix-socket broker for one disposable container per compute job."""
# ruff: noqa: RUF001 -- Chinese compute messages use localized punctuation.
from __future__ import annotations

import argparse
import json
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


class ComputeBroker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = False

    def __init__(self, socket_path: Path, image: str, market_root: Path, capacity: int):
        self.image = image
        self.market_root = market_root
        self.permits = threading.BoundedSemaphore(capacity)
        self.tenant_lock = threading.Lock()
        self.active_tenants: set[str] = set()
        self.stopped = threading.Event()
        super().__init__(str(socket_path), Handler)

    def command(self, name: str, market_path: Path | None = None) -> list[str]:
        # No host credentials, private files, Docker socket, network, or writable
        # shared mounts enter this container. env -i also removes image defaults.
        return [
            "docker", "run", "--rm", "--name", name, "-i", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "10001:10001", "--pids-limit", "128", "--memory", "4g",
            "--memory-swap", "4g", "--cpus", "2", "--ulimit", "nofile=256:256",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,uid=10001,gid=10001",
            "--mount", f"type=bind,src={market_path or self.market_root},dst=/market,readonly",
            "--entrypoint", "/usr/bin/env", self.image, "-i",
            "PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONPATH=/app",
            "PYTHONDONTWRITEBYTECODE=1", "HOME=/tmp", "XDG_CACHE_HOME=/tmp/xdg",
            "DATA_DIR=/market", "TICKFLOW_ENV_FILE=/dev/null",
            "TIERS_YAML=/app/tiers.yaml", "TZ=Asia/Shanghai", "POLARS_MAX_THREADS=2",
            "NUMBA_NUM_THREADS=2", "OPENBLAS_NUM_THREADS=1", "OMP_NUM_THREADS=1",
            "NUMBA_CACHE_DIR=/tmp/numba", "/app/.venv/bin/python", "-m",
            "app.multitenancy.compute_worker",
        ]


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
        try:
            self.connection.settimeout(30)
            line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            if len(line) > MAX_MESSAGE_BYTES or not line.endswith(b"\n"):
                raise ValueError("计算任务输入过大或不完整")
            payload = json.loads(line)
            from app.multitenancy.publication import release_path

            market_path = release_path(broker.market_root, payload.get("public_release_id"))
            if payload.get("kind") == "health":
                checked = subprocess.run(["docker", "image", "inspect", broker.image],
                                         capture_output=True, timeout=3, check=False)
                self.send({"type": "result", "payload": {"ready": checked.returncode == 0}})
                return
            tenant = payload.get("tenant_id")
            if (payload.get("kind") not in KINDS or not isinstance(tenant, str)
                    or not 1 <= len(tenant) <= 128):
                raise ValueError("计算任务格式错误")
            if not broker.permits.acquire(blocking=False):
                self.send({"type": "error", "status": 429, "message": "计算任务繁忙，请稍后重试"})
                return
            acquired = True
            with broker.tenant_lock:
                if tenant in broker.active_tenants:
                    self.send({"type": "error", "status": 429, "message": "当前租户已有计算任务，请稍后重试"})
                    tenant = None
                    return
                broker.active_tenants.add(tenant)
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
                    process.communicate(timeout=5)
            if tenant is not None and acquired:
                with broker.tenant_lock:
                    broker.active_tenants.discard(tenant)
            if acquired:
                broker.permits.release()
            # The next task may start as soon as the client receives a terminal
            # event. Release both permits after cleanup, before sending it.
            if terminal_message is not None:
                with suppress(OSError):
                    self.send(terminal_message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--market-root", type=Path, required=True)
    parser.add_argument("--capacity", type=int, default=4)
    args = parser.parse_args()
    market = args.market_root.resolve(strict=True)
    if not market.is_dir() or not 1 <= args.capacity <= 16:
        raise ValueError("invalid compute broker configuration")
    if (market / "user_data").exists() or (market / ".env").exists():
        raise ValueError("compute market mount must not contain private state or credentials")
    args.socket.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    # A stale socket is removed by the service manager, never blindly unlink an
    # active broker's socket at startup.
    with ComputeBroker(args.socket, args.image, market, args.capacity) as server:
        os.chmod(args.socket, 0o660)
        def shutdown(*_):
            server.stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, shutdown)
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
