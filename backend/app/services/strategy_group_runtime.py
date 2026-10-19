"""Latest-only coordinator for one logical strategy group.

The coordinator owns scheduling and cycle publication only. It never reads
market data or places orders; callers provide one immutable snapshot payload
and a synchronous, side-effect-free handler. At most one snapshot is active
and one is pending. Consumers perform notifications or orders only after
reading a successfully published result. An optional publisher may commit
in-memory calculation state after freshness checks; it must not send
notifications, place orders, or perform other external side effects.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Generic, TypeVar

logger = logging.getLogger(__name__)

PayloadT = TypeVar("PayloadT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class StrategyGroupSnapshot(Generic[PayloadT]):
    group_id: str
    cycle_id: str
    sequence: int
    captured_at: float
    submitted_at: float
    identity: str | None
    payload: PayloadT


@dataclass(frozen=True)
class StrategyGroupCycle:
    group_id: str
    cycle_id: str
    sequence: int
    identity: str | None
    state: str
    captured_at: float
    submitted_at: float
    started_at: float | None = None
    finished_at: float | None = None
    queue_wait_ms: float | None = None
    elapsed_ms: float | None = None
    snapshot_age_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _WorkItem(Generic[PayloadT]):
    snapshot: StrategyGroupSnapshot[PayloadT]
    cycle: StrategyGroupCycle
    submitted_monotonic: float


class StrategyGroupCoordinator(Generic[PayloadT, ResultT]):
    """Run one snapshot at a time and coalesce pending work to the latest."""

    def __init__(
        self,
        handler: Callable[[StrategyGroupSnapshot[PayloadT]], ResultT],
        *,
        group_id: str,
        publisher: Callable[[StrategyGroupSnapshot[PayloadT], ResultT], ResultT] | None = None,
        thread_name: str = "strategy-group",
        stale_after_s: float | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not group_id.strip():
            raise ValueError("group_id不能为空")
        if stale_after_s is not None and stale_after_s <= 0:
            raise ValueError("stale_after_s必须为正数")
        self.group_id = group_id
        self._handler = handler
        self._publisher = publisher
        self._thread_name = thread_name
        self._stale_after_s = stale_after_s
        self._clock = clock
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._sequence = 0
        self._accepting = True
        self._stopping = False
        self._drain = True
        self._worker: threading.Thread | None = None
        self._pending: _WorkItem[PayloadT] | None = None
        self._active: StrategyGroupCycle | None = None
        self._latest: StrategyGroupCycle | None = None
        self._latest_success: tuple[StrategyGroupCycle, ResultT] | None = None
        self._counts = {
            "submitted": 0,
            "started": 0,
            "succeeded": 0,
            "failed": 0,
            "stale": 0,
            "interrupted": 0,
            "superseded": 0,
        }

    def submit(
        self,
        payload: PayloadT,
        *,
        captured_at: float | None = None,
        identity: str | None = None,
    ) -> StrategyGroupSnapshot[PayloadT]:
        now = self._clock()
        submitted_monotonic = self._monotonic()
        with self._condition:
            if not self._accepting:
                raise RuntimeError("策略组协调器已停止")
            self._sequence += 1
            sequence = self._sequence
            cycle_id = f"{self.group_id}:{sequence}"
            snapshot = StrategyGroupSnapshot(
                group_id=self.group_id,
                cycle_id=cycle_id,
                sequence=sequence,
                captured_at=now if captured_at is None else float(captured_at),
                submitted_at=now,
                identity=identity,
                payload=payload,
            )
            cycle = StrategyGroupCycle(
                group_id=self.group_id,
                cycle_id=cycle_id,
                sequence=sequence,
                identity=identity,
                state="queued",
                captured_at=snapshot.captured_at,
                submitted_at=now,
            )
            if self._pending is not None:
                self._counts["superseded"] += 1
            self._pending = _WorkItem(snapshot, cycle, submitted_monotonic)
            self._counts["submitted"] += 1
            self._ensure_worker_locked()
            self._condition.notify_all()
            return snapshot

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else self._monotonic() + max(0.0, timeout)
        with self._condition:
            while self._active is not None or self._pending is not None:
                remaining = None if deadline is None else deadline - self._monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def latest_success(self) -> tuple[StrategyGroupCycle, ResultT] | None:
        with self._condition:
            return self._latest_success

    def status(self) -> dict:
        with self._condition:
            worker = self._worker
            return {
                "group_id": self.group_id,
                "accepting": self._accepting,
                "running": bool(worker and worker.is_alive()),
                "active": self._active.to_dict() if self._active else None,
                "pending": self._pending.cycle.to_dict() if self._pending else None,
                "latest": self._latest.to_dict() if self._latest else None,
                "latest_success": (
                    self._latest_success[0].to_dict()
                    if self._latest_success is not None else None
                ),
                "counts": dict(self._counts),
            }

    def stop(self, *, drain: bool = True, timeout: float = 10.0) -> bool:
        with self._condition:
            self._accepting = False
            self._stopping = True
            self._drain = drain
            if not drain and self._pending is not None:
                now = self._clock()
                interrupted = replace(
                    self._pending.cycle,
                    state="interrupted",
                    finished_at=now,
                    snapshot_age_ms=max(0.0, now - self._pending.cycle.captured_at) * 1000,
                    error="协调器停止前未开始",
                )
                self._latest = interrupted
                self._pending = None
                self._counts["interrupted"] += 1
            worker = self._worker
            self._condition.notify_all()
        if worker is None:
            return True
        worker.join(timeout=max(0.0, timeout))
        return not worker.is_alive()

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._run,
            name=self._thread_name,
            daemon=True,
        )
        self._worker.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                while self._pending is None and not self._stopping:
                    self._condition.wait()
                if self._pending is None and self._stopping:
                    break
                if self._stopping and not self._drain:
                    break
                item = self._pending
                self._pending = None
                if item is None:
                    continue
                started_at = self._clock()
                started_monotonic = self._monotonic()
                running = replace(
                    item.cycle,
                    state="running",
                    started_at=started_at,
                    queue_wait_ms=max(
                        0.0,
                        started_monotonic - item.submitted_monotonic,
                    ) * 1000,
                )
                self._active = running
                self._counts["started"] += 1
                self._condition.notify_all()

            result = None
            error = None
            stale = (
                self._stale_after_s is not None
                and started_at - item.snapshot.captured_at > self._stale_after_s
            )
            if not stale:
                try:
                    result = self._handler(item.snapshot)
                except Exception as exc:
                    error = str(exc)
                    logger.exception(
                        "strategy group cycle failed group=%s cycle=%s",
                        self.group_id,
                        item.snapshot.cycle_id,
                    )

            computed_at = self._clock()
            snapshot_age_ms = max(0.0, computed_at - running.captured_at) * 1000
            stale = stale or (
                error is None
                and self._stale_after_s is not None
                and snapshot_age_ms > self._stale_after_s * 1000
            )
            if error is None and not stale and self._publisher is not None:
                try:
                    result = self._publisher(item.snapshot, result)
                except Exception as exc:
                    error = str(exc)
                    logger.exception(
                        "strategy group publication failed group=%s cycle=%s",
                        self.group_id,
                        item.snapshot.cycle_id,
                    )
            finished_at = self._clock()
            finished_monotonic = self._monotonic()
            terminal = replace(
                running,
                state="failed" if error is not None else "stale" if stale else "succeeded",
                finished_at=finished_at,
                elapsed_ms=max(0.0, finished_monotonic - started_monotonic) * 1000,
                snapshot_age_ms=snapshot_age_ms,
                error=error or ("快照超过有效期" if stale else None),
            )
            with self._condition:
                self._active = None
                self._latest = terminal
                if error is None and not stale:
                    self._latest_success = (terminal, result)
                    self._counts["succeeded"] += 1
                elif stale:
                    self._counts["stale"] += 1
                else:
                    self._counts["failed"] += 1
                self._condition.notify_all()
                if self._stopping and self._pending is None:
                    break

        with self._condition:
            self._condition.notify_all()
