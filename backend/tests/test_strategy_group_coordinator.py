from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.strategy_group_runtime import StrategyGroupCoordinator


def test_coordinator_publishes_success_atomically() -> None:
    coordinator = StrategyGroupCoordinator(
        lambda snapshot: {"value": snapshot.payload * 2},
        group_id="primary",
    )
    try:
        submitted = coordinator.submit(
            21,
            captured_at=time.time() - 0.01,
            identity="release-1",
        )
        assert coordinator.wait_idle(timeout=2)

        latest = coordinator.latest_success()
        assert latest is not None
        cycle, result = latest
        assert cycle.cycle_id == submitted.cycle_id
        assert cycle.state == "succeeded"
        assert cycle.identity == "release-1"
        assert cycle.queue_wait_ms is not None
        assert cycle.elapsed_ms is not None
        assert cycle.snapshot_age_ms is not None
        assert result == {"value": 42}

        status = coordinator.status()
        assert status["active"] is None
        assert status["pending"] is None
        assert status["latest"]["cycle_id"] == submitted.cycle_id
        assert status["latest_success"]["cycle_id"] == submitted.cycle_id
        assert status["counts"] == {
            "submitted": 1,
            "started": 1,
            "succeeded": 1,
            "failed": 0,
            "stale": 0,
            "interrupted": 0,
            "superseded": 0,
        }
    finally:
        assert coordinator.stop(timeout=2)


def test_coordinator_runs_in_memory_publisher_before_success_is_visible() -> None:
    published: list[tuple[str, int]] = []

    def publish(snapshot, result):
        published.append((snapshot.cycle_id, result))
        return {"published": result}

    coordinator = StrategyGroupCoordinator(
        lambda snapshot: snapshot.payload * 2,
        publisher=publish,
        group_id="primary",
    )
    try:
        submitted = coordinator.submit(21)
        assert coordinator.wait_idle(timeout=2)

        latest = coordinator.latest_success()
        assert latest is not None
        assert latest[0].cycle_id == submitted.cycle_id
        assert latest[1] == {"published": 42}
        assert published == [(submitted.cycle_id, 42)]
    finally:
        assert coordinator.stop(timeout=2)


def test_coordinator_mailbox_keeps_only_latest_pending_snapshot() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    processed: list[int] = []

    def handle(snapshot):
        processed.append(snapshot.payload)
        if snapshot.payload == 1:
            first_started.set()
            assert release_first.wait(2)
        return snapshot.payload

    coordinator = StrategyGroupCoordinator(handle, group_id="primary")
    try:
        coordinator.submit(1)
        assert first_started.wait(1)
        coordinator.submit(2)
        latest = coordinator.submit(3)
        release_first.set()
        assert coordinator.wait_idle(timeout=2)

        assert processed == [1, 3]
        assert coordinator.latest_success()[0].cycle_id == latest.cycle_id
        assert coordinator.status()["counts"]["superseded"] == 1
    finally:
        release_first.set()
        assert coordinator.stop(timeout=2)


def test_coordinator_failure_isolated_and_previous_success_is_retained() -> None:
    def handle(snapshot):
        if snapshot.payload == "bad":
            raise ValueError("injected failure")
        return snapshot.payload.upper()

    coordinator = StrategyGroupCoordinator(handle, group_id="primary")
    try:
        good = coordinator.submit("good")
        assert coordinator.wait_idle(timeout=2)
        assert coordinator.latest_success()[0].cycle_id == good.cycle_id

        bad = coordinator.submit("bad")
        assert coordinator.wait_idle(timeout=2)
        latest_success = coordinator.latest_success()
        assert latest_success[0].cycle_id == good.cycle_id
        assert latest_success[1] == "GOOD"

        status = coordinator.status()
        assert status["latest"]["cycle_id"] == bad.cycle_id
        assert status["latest"]["state"] == "failed"
        assert status["latest"]["error"] == "injected failure"
        assert status["latest_success"]["cycle_id"] == good.cycle_id

        recovered = coordinator.submit("next")
        assert coordinator.wait_idle(timeout=2)
        assert coordinator.latest_success()[0].cycle_id == recovered.cycle_id
        assert coordinator.latest_success()[1] == "NEXT"
        assert coordinator.status()["counts"]["failed"] == 1
    finally:
        assert coordinator.stop(timeout=2)


def test_coordinator_stop_without_drain_interrupts_pending_snapshot() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    processed: list[int] = []

    def handle(snapshot):
        processed.append(snapshot.payload)
        first_started.set()
        assert release_first.wait(2)
        return snapshot.payload

    coordinator = StrategyGroupCoordinator(handle, group_id="primary")
    coordinator.submit(1)
    assert first_started.wait(1)
    coordinator.submit(2)

    assert coordinator.stop(drain=False, timeout=0.01) is False
    status = coordinator.status()
    assert status["pending"] is None
    assert status["counts"]["interrupted"] == 1

    release_first.set()
    assert coordinator.stop(drain=False, timeout=2)
    assert processed == [1]
    with pytest.raises(RuntimeError, match="已停止"):
        coordinator.submit(3)


def test_coordinator_stop_with_drain_finishes_pending_snapshot() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    processed: list[int] = []

    def handle(snapshot):
        processed.append(snapshot.payload)
        if snapshot.payload == 1:
            first_started.set()
            assert release_first.wait(2)
        return snapshot.payload

    coordinator = StrategyGroupCoordinator(handle, group_id="primary")
    coordinator.submit(1)
    assert first_started.wait(1)
    latest = coordinator.submit(2)

    assert coordinator.stop(drain=True, timeout=0.01) is False
    release_first.set()
    assert coordinator.stop(drain=True, timeout=2)

    assert processed == [1, 2]
    assert coordinator.latest_success()[0].cycle_id == latest.cycle_id
    assert coordinator.status()["counts"]["succeeded"] == 2


def test_coordinator_does_not_execute_or_publish_stale_snapshot() -> None:
    called = False

    def handle(snapshot):
        nonlocal called
        called = True
        return snapshot.payload

    coordinator = StrategyGroupCoordinator(
        handle,
        group_id="primary",
        stale_after_s=0.1,
    )
    try:
        submitted = coordinator.submit(1, captured_at=time.time() - 1)
        assert coordinator.wait_idle(timeout=2)

        assert called is False
        assert coordinator.latest_success() is None
        status = coordinator.status()
        assert status["latest"]["cycle_id"] == submitted.cycle_id
        assert status["latest"]["state"] == "stale"
        assert status["latest"]["error"] == "快照超过有效期"
        assert status["counts"]["stale"] == 1
    finally:
        assert coordinator.stop(timeout=2)


def test_coordinator_does_not_publish_result_that_becomes_stale_during_execution() -> None:
    wall_clock = [100.0]
    published: list[int] = []

    def handle(snapshot):
        wall_clock[0] += 1.0
        return snapshot.payload

    coordinator = StrategyGroupCoordinator(
        handle,
        publisher=lambda _snapshot, result: published.append(result),
        group_id="primary",
        stale_after_s=0.1,
        clock=lambda: wall_clock[0],
    )
    try:
        submitted = coordinator.submit(1)
        assert coordinator.wait_idle(timeout=2)

        assert coordinator.latest_success() is None
        status = coordinator.status()
        assert status["latest"]["cycle_id"] == submitted.cycle_id
        assert status["latest"]["state"] == "stale"
        assert status["counts"]["stale"] == 1
        assert published == []
    finally:
        assert coordinator.stop(timeout=2)


def test_coordinator_concurrent_submit_keeps_unique_sequence_and_latest() -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    processed: list[int] = []

    def handle(snapshot):
        processed.append(snapshot.payload)
        if snapshot.payload == 0:
            first_started.set()
            assert release_first.wait(2)
        return snapshot.payload

    coordinator = StrategyGroupCoordinator(handle, group_id="primary")
    try:
        coordinator.submit(0)
        assert first_started.wait(1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            snapshots = list(pool.map(coordinator.submit, range(1, 101)))
        latest = max(snapshots, key=lambda item: item.sequence)
        assert len({item.sequence for item in snapshots}) == 100

        release_first.set()
        assert coordinator.wait_idle(timeout=2)
        assert processed == [0, latest.payload]
        status = coordinator.status()
        assert status["counts"]["submitted"] == 101
        assert status["counts"]["superseded"] == 99
        assert status["latest_success"]["cycle_id"] == latest.cycle_id
    finally:
        release_first.set()
        assert coordinator.stop(timeout=2)
