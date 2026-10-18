"""Supervise a Compose worker using progress from its actual dispatch loop."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

MODULES = {
    "research": "app.multitenancy.research_worker",
    "realtime": "app.multitenancy.runtime",
    "notifications": "app.multitenancy.notification_worker",
    "scheduled": "app.multitenancy.scheduled_worker",
}
# Realtime may await a 300s compute timeout; scheduled review allows 300s.
MAX_AGE = {"research": 120, "realtime": 420, "notifications": 120, "scheduled": 660}
logger = logging.getLogger(__name__)


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def record(kind: str, healthy: bool) -> None:
    directory = os.environ.get("TSP_WORKER_HEARTBEAT_DIR")
    instance = os.environ.get("TSP_WORKER_INSTANCE")
    if directory and instance:
        write_json(Path(directory) / "progress.json", {
            "kind": kind, "pid": os.getpid(), "instance": instance,
            "at": time.monotonic(), "healthy": healthy,
        })


def progress(directory: Path, pid: int, instance: str, kind: str) -> float | None:
    try:
        value = json.loads((directory / "progress.json").read_text())
        if (value["pid"] != pid or value["instance"] != instance
                or value["kind"] != kind or value["healthy"] is not True):
            return None
        at = float(value["at"])
        return at if math.isfinite(at) and 0 <= at <= time.monotonic() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def check(directory: Path, kind: str, *, max_age: float) -> bool:
    try:
        owner = json.loads((directory / "owner.json").read_text())
        os.kill(owner["pid"], 0)
        at = progress(directory, owner["pid"], owner["instance"], kind)
        return at is not None and time.monotonic() - at <= max_age
    except (OSError, ValueError, KeyError, TypeError):
        return False


def supervise(command: list[str], *, kind: str, directory: Path, max_age: float,
              startup: float = 120, grace: float = 25, poll: float = .5) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    instance = uuid.uuid4().hex
    stopped = threading.Event()
    previous = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, lambda *_: stopped.set())
    child = None
    try:
        child = subprocess.Popen(command, env={
            **os.environ, "TSP_WORKER_HEARTBEAT_DIR": str(directory),
            "TSP_WORKER_INSTANCE": instance,
        })
        write_json(directory / "owner.json", {"pid": child.pid, "instance": instance})
        deadline = time.monotonic() + startup
        while child.poll() is None:
            if stopped.wait(poll):
                return 0
            at = progress(directory, child.pid, instance, kind)
            if at is not None:
                deadline = at + max_age
            if time.monotonic() > deadline:
                logger.error("%s dispatch progress expired; stopping worker", kind)
                return 1
        # A worker must remain running until requested to stop.
        return child.returncode or 1
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=MODULES)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--directory", type=Path, default=Path("/tmp/tsp-worker"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    age = MAX_AGE[args.kind]
    if args.check:
        raise SystemExit(0 if check(args.directory, args.kind, max_age=age) else 1)
    raise SystemExit(supervise(
        [sys.executable, "-m", MODULES[args.kind]],
        kind=args.kind, directory=args.directory, max_age=age,
    ))


if __name__ == "__main__":
    main()
