"""Memory available to one service, including explicit native-host budgets."""
from __future__ import annotations

import os
from pathlib import Path

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_CGROUP_MEMBERSHIP = Path("/proc/self/cgroup")
MIB = 1024**2


def _physical_memory_bytes() -> int | None:
    try:
        import psutil

        return psutil.virtual_memory().total
    except (ImportError, OSError, RuntimeError):
        return None


def _read_limit(path: Path) -> int | None:
    try:
        value = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    # cgroup v1 represents unlimited memory with a near-LONG_MAX value.
    return value if 0 < value < 2**60 else None


def _cgroup_limits() -> list[int]:
    locations = {
        (_CGROUP_ROOT, "memory.max"),
        (_CGROUP_ROOT / "memory", "memory.limit_in_bytes"),
    }
    try:
        for line in _CGROUP_MEMBERSHIP.read_text().splitlines():
            _, controllers, group = line.split(":", 2)
            relative = Path(group.lstrip("/"))
            if ".." in relative.parts:
                continue
            if not controllers:
                root, filename = _CGROUP_ROOT, "memory.max"
            elif "memory" in controllers.split(","):
                root, filename = _CGROUP_ROOT / "memory", "memory.limit_in_bytes"
            else:
                continue
            current = root / relative
            while current != root:
                locations.add((current, filename))
                current = current.parent
    except (OSError, ValueError):
        pass
    return [value for root, name in locations if (value := _read_limit(root / name)) is not None]


def effective_memory_bytes() -> int | None:
    """Return the tightest known limit, not free memory or an allocation promise.

    TSP_MEMORY_BUDGET_MB lets native Windows services share a host budget.
    It steers batching/caches; only an OS job/cgroup provides a hard limit.
    Read limits on demand so a changed container budget takes effect.
    """
    limits = _cgroup_limits()
    physical = _physical_memory_bytes()
    if physical is not None and physical > 0:
        limits.append(physical)
    configured = os.environ.get("TSP_MEMORY_BUDGET_MB")
    if configured is not None:
        try:
            budget = int(configured)
        except ValueError as exc:
            raise ValueError("TSP_MEMORY_BUDGET_MB must be a positive integer") from exc
        if budget <= 0:
            raise ValueError("TSP_MEMORY_BUDGET_MB must be a positive integer")
        limits.append(budget * MIB)
    return min(limits) if limits else None


def cache_budget_bytes(maximum_mb: int, *, fraction: int = 8) -> int:
    """Bound retained data while reserving space for computation and the runtime."""
    maximum = maximum_mb * MIB
    memory = effective_memory_bytes()
    return min(maximum, max(MIB, memory // fraction)) if memory else maximum
