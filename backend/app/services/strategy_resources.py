"""Shared strategy budget calculation and bounded admission.

The broker applies allocations as Docker hard limits. Standalone runtimes use
the same concurrency policy, but their in-process memory estimate is advisory.
"""
from __future__ import annotations

import inspect
import json
import math
import os
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

MAX_CONCURRENCY = 16
MIN_TASK_MEMORY_MB = 256
MIN_TASK_CPUS = .25
BUDGET_RATIO = .25


def host_resources() -> tuple[int, float]:
    """Physical resources on the execution host, never browser/API pod memory."""
    memory_mb = 0
    try:
        memory_mb = int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // 1048576)
    except (OSError, ValueError, AttributeError):
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
                            *[(name, ctypes.c_ulonglong) for name in (
                                "physical", "available", "pagefile", "available_pagefile",
                                "virtual", "available_virtual", "extended",
                            )]]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                memory_mb = int(status.physical // 1048576)
        else:
            try:
                result = subprocess.run(
                    ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                    timeout=2, check=True,
                )
                memory_mb = int(result.stdout.strip()) // 1048576
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
    return memory_mb, float(os.cpu_count() or 1)


def budget_status(
    memory_mb: int, cpus: float, concurrency: int | None, *,
    task_memory_mb: int = 4096, task_cpus: float = 2, ceiling: int = MAX_CONCURRENCY,
) -> dict:
    memory_budget = int(memory_mb * BUDGET_RATIO)
    cpu_budget = cpus * BUDGET_RATIO
    maximum = max(0, min(
        ceiling, memory_budget // MIN_TASK_MEMORY_MB,
        math.floor(cpu_budget / MIN_TASK_CPUS),
    ))
    recommended = 0 if maximum == 0 else max(1, min(
        maximum, memory_budget // task_memory_mb, math.floor(cpu_budget / task_cpus),
    ))
    effective = recommended if concurrency is None else min(concurrency, maximum)
    # Round down to Docker's decimal CPU quota so N allocations stay in budget.
    allocated_cpus = (
        math.floor(min(task_cpus, cpu_budget / effective) * 1_000_000) / 1_000_000
        if effective else 0
    )
    return {
        "host_memory_mb": memory_mb,
        "host_cpus": cpus,
        "budget_ratio": BUDGET_RATIO,
        "memory_budget_mb": memory_budget,
        "cpu_budget": cpu_budget,
        "recommended_concurrency": recommended,
        "max_concurrency": maximum,
        "strategy_concurrency": concurrency,
        "effective_concurrency": effective,
        "task_memory_mb": min(task_memory_mb, memory_budget // effective) if effective else 0,
        "task_cpus": allocated_cpus,
        "recommendation_basis": "task_limits",
    }


class BudgetPermits:
    """Live limits without replacing a semaphore or losing old allocations."""

    def __init__(self, status: Callable[[], dict]):
        self.status = status
        self.condition = threading.Condition()
        self.allocations: dict[int, list[tuple[int, float]]] = {}
        self.active = 0
        self.memory_mb = 0
        self.cpus = 0.0

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            # Filesystem/configuration reads stay outside the admission lock.
            status = self.status()
            memory, cpus = status["task_memory_mb"], status["task_cpus"]
            with self.condition:
                if (self.active < status["effective_concurrency"]
                        and self.memory_mb + memory <= status["memory_budget_mb"]
                        and self.cpus + cpus <= status["cpu_budget"] + 1e-9):
                    self.allocations.setdefault(threading.get_ident(), []).append((memory, cpus))
                    self.active += 1
                    self.memory_mb += memory
                    self.cpus += cpus
                    return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if not blocking or (remaining is not None and remaining <= 0):
                    return False
                self.condition.wait(.25 if remaining is None else min(.25, remaining))

    def allocation(self) -> tuple[int, float] | None:
        with self.condition:
            values = self.allocations.get(threading.get_ident(), [])
            return values[-1] if values else None

    def release(self) -> None:
        with self.condition:
            values = self.allocations.get(threading.get_ident(), [])
            if not values:
                raise ValueError("no compute permit held by this thread")
            memory, cpus = values.pop()
            if not values:
                self.allocations.pop(threading.get_ident(), None)
            self.active -= 1
            self.memory_mb -= memory
            self.cpus = max(0.0, self.cpus - cpus)
            self.condition.notify_all()

    def usage(self) -> dict:
        with self.condition:
            return {"active": self.active, "reserved_memory_mb": self.memory_mb,
                    "reserved_cpus": self.cpus}


class ConcurrencyConfig:
    """Only the trusted host configuration file can change admission policy."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.value: int | None = None

    def read(self) -> int | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            value = data.get("strategy_concurrency")
            if value is not None and (type(value) is not int or not 1 <= value <= MAX_CONCURRENCY):
                raise ValueError("invalid strategy concurrency")
        except (OSError, ValueError, AttributeError):
            with self.lock:
                return self.value
        with self.lock:
            self.value = value
        return value


_local_host: tuple[int, float] | None = None
_preference_config: ConcurrencyConfig | None = None
_preference_config_lock = threading.Lock()


def configured_concurrency() -> int | None:
    from app.services import preferences

    global _preference_config
    path = preferences._path()
    with _preference_config_lock:
        if _preference_config is None or _preference_config.path != path:
            _preference_config = ConcurrencyConfig(path)
        config = _preference_config
    return config.read()


def local_status() -> dict:
    global _local_host

    if _local_host is None:
        _local_host = host_resources()
    return {**budget_status(*_local_host, configured_concurrency()), "config_enabled": True,
            "execution_mode": "in_process", **LOCAL_PERMITS.usage()}


LOCAL_PERMITS = BudgetPermits(local_status)
_local_depth = threading.local()


@contextmanager
def local_slot():
    from app.cell.context import AccessError
    from app.config import settings

    # Remote execution is admitted once by the one host broker.
    if settings.multitenancy_enabled or settings.service_role != "standalone":
        yield
        return
    depth = getattr(_local_depth, "value", 0)
    if depth:
        yield
        return
    if not LOCAL_PERMITS.acquire(timeout=2):
        raise AccessError(429, "策略任务繁忙, 请稍后重试")
    _local_depth.value = 1
    try:
        yield
    finally:
        _local_depth.value = 0
        LOCAL_PERMITS.release()


def limit_local_strategy(function):
    """Apply before context/matrix preparation, including background batches."""
    @wraps(function)
    def run(*args, **kwargs):
        with local_slot():
            return function(*args, **kwargs)

    # FastAPI must resolve annotations in the original endpoint's globals.
    run.__signature__ = inspect.signature(function, eval_str=True)
    return run
