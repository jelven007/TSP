"""Latency regressions exercise publication and admission without market requests."""
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.multitenancy.publication import PublicPublisher, release_path
from app.services.minute_refresh import MinuteRefreshService
from app.services.strategy_resources import BudgetPermits, budget_status


def test_completed_minute_round_notifies_publisher_after_write(tmp_path, monkeypatch):
    calls = []
    publisher = SimpleNamespace(request_minute_publish=lambda at: calls.append(("publish", at)))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    service = MinuteRefreshService(repo)
    service.set_app_state(SimpleNamespace(public_publisher=publisher))
    monkeypatch.setattr(service, "_resolve_custom", lambda: (None, "tickflow"))
    monkeypatch.setattr(service, "_select_mode", lambda: "increment")
    minute = datetime(2026, 11, 4, 14, 31)
    frame = pl.DataFrame({"symbol": ["600000.SH"], "datetime": [minute]})
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_universe_increment",
                        lambda: (frame, 1))
    monkeypatch.setattr("app.services.minute_refresh.cn_now", lambda: minute)
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition",
                        lambda *_: calls.append(("write", None)) or 1)
    service._run_round()
    assert calls == [("write", None), ("publish", minute)]
    assert service._state.last_error is None and service._state.rounds == 1
    assert service._state.last_fetch_ms >= 0 and service._state.last_write_ms >= 0


def test_empty_minute_round_never_notifies_publisher(tmp_path, monkeypatch):
    calls = []
    service = MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    service.set_app_state(SimpleNamespace(public_publisher=SimpleNamespace(
        request_minute_publish=lambda at: calls.append(at))))
    monkeypatch.setattr(service, "_resolve_custom", lambda: (None, "tickflow"))
    monkeypatch.setattr(service, "_select_mode", lambda: "increment")
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_universe_increment",
                        lambda: (pl.DataFrame(), 1))
    service._run_round()
    assert calls == []


def test_minute_publication_wakes_loop_and_preserves_immutable_release(tmp_path):
    source, public = tmp_path / "source", tmp_path / "public"
    part = source / "kline_minute" / "date=2026-11-04" / "part.parquet"
    part.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "close": [10.]}).write_parquet(part)
    from app.enriched_generation import get_enriched_generation

    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    publisher = PublicPublisher(source, public)
    first = publisher.publish()
    publisher.thread.start()
    try:
        pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "close": [11., 12.]}).write_parquet(part)
        # The test waits for the actual publish boundary, not a guessed sleep.
        import threading

        ready = threading.Event()
        original = publisher._swap_current
        # Check already-published state too: the background thread may win.
        publisher._swap_current = lambda link: (original(link), ready.set())
        publisher.request_minute_publish(datetime(2026, 11, 4, 14, 31, 28))
        if release_path(public).name == first["id"]:
            assert ready.wait(3), "complete minutes still wait for the 300-second poll"
        assert release_path(public).name != first["id"]
        assert pl.read_parquet(release_path(public) / part.relative_to(source)).height == 2
        assert pl.read_parquet(release_path(public, first["id"]) / part.relative_to(source)).height == 1
    finally:
        publisher.close()


def test_outside_tail_window_does_not_request_frequent_file_releases(tmp_path):
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    publisher.request_minute_publish(datetime(2026, 11, 4, 10))
    assert not publisher.changed.is_set()
    publisher.request_minute_publish(datetime(2026, 11, 4, 14, 31))
    assert publisher.changed.is_set()


def test_monitor_keeps_reserved_capacity_when_research_is_running():
    status = {**budget_status(16384, 8, 2), "realtime_reserved_slots": 1}
    permits = BudgetPermits(lambda: status)
    assert permits.acquire(blocking=False)
    try:
        assert not permits.acquire(blocking=False), "research consumed the realtime reserve"
        assert permits.acquire(blocking=False, priority=True)
        permits.release()
    finally:
        permits.release()
    assert permits.usage()["active"] == 0


def test_timed_out_priority_waiter_does_not_block_later_research():
    permits = BudgetPermits(lambda: budget_status(8192, 4, 1))
    assert permits.acquire(blocking=False)
    try:
        assert not permits.acquire(timeout=.01, priority=True)
    finally:
        permits.release()
    assert permits.acquire(blocking=False)
    permits.release()
    assert permits.usage()["active"] == 0


def test_broker_reserves_one_slot_only_in_tail_window(tmp_path, monkeypatch):
    import tempfile
    from pathlib import Path

    from app.multitenancy.compute_broker import ComputeBroker

    with (tempfile.TemporaryDirectory(prefix="tsp-ipc-", dir="/tmp") as directory,
          ComputeBroker(Path(directory) / "compute.sock", "pinned", tmp_path, 4,
                        host_memory_mb=32768, host_cpus=16) as broker):
        monkeypatch.setattr("app.multitenancy.compute_broker.cn_now",
                            lambda: datetime(2026, 11, 4, 14, 31), raising=False)
        assert broker.resource_status()["realtime_reserved_slots"] == 1
        monkeypatch.setattr("app.multitenancy.compute_broker.cn_now",
                            lambda: datetime(2026, 11, 4, 10))
        assert broker.resource_status()["realtime_reserved_slots"] == 0


def test_api_binding_polls_new_release_within_one_second():
    from app.multitenancy.public_binding_sync import PublicBindingSynchronizer

    assert PublicBindingSynchronizer(None).interval == 1


def test_public_pool_history_reads_only_reference_window_and_reuses_it(tmp_path, monkeypatch):
    from datetime import date, timedelta

    from app.services.screener import ScreenerService

    (tmp_path / ".public_release.json").write_text('{"id":"immutable"}')
    for offset in range(8):
        day = date(2026, 10, 26) + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        part = tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet"
        part.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "date": [day, day],
                      "raw_close": [10. + offset, 12.], "volume": [10., 0.],
                      "turnover_rate": [1., 0.]}).write_parquet(part)
    service = ScreenerService(SimpleNamespace(store=SimpleNamespace(
        data_dir=tmp_path, read_only=True)))
    monkeypatch.setattr(service, "_load_enriched_history",
                        lambda *_: (_ for _ in ()).throw(AssertionError("pool rebuilt indicators")))
    first = service._load_pool_history(date(2026, 11, 2), 2)
    assert first["date"].unique().sort().to_list() == [
        date(2026, 10, 29), date(2026, 10, 30), date(2026, 11, 2)]
    assert set(first["symbol"]) == {"600000.SH", "000001.SZ"}
    assert first["volume"].to_list() and "turnover_rate" not in first.columns
    from app.strategy.suspension import COLUMN, annotate

    status = annotate(first, as_of=date(2026, 11, 2)).filter(pl.col("date") == date(2026, 11, 2))
    assert dict(zip(status["symbol"], status[COLUMN], strict=True)) == {
        "600000.SH": False, "000001.SZ": None,
    }
    # Release contents are immutable: a second call should not scan again.
    monkeypatch.setattr("app.services.screener.scan_enriched_parquet",
                        lambda *_: (_ for _ in ()).throw(AssertionError("reference scanned twice")))
    assert service._load_pool_history(date(2026, 11, 2), 2).equals(first)


def test_pool_history_cache_never_crosses_public_release(tmp_path):
    from datetime import date

    from app.services.screener import ScreenerService

    values = []
    for name, close in [("first", 10.), ("second", 20.)]:
        root = tmp_path / name
        part = root / "kline_daily_enriched" / "date=2026-11-02" / "part.parquet"
        part.parent.mkdir(parents=True)
        (root / ".public_release.json").write_text('{"id":"' + name + '"}')
        pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 11, 2)],
                      "raw_close": [close]}).write_parquet(part)
        values.append(ScreenerService(SimpleNamespace(store=SimpleNamespace(
            data_dir=root, read_only=True)))._load_pool_history(date(2026, 11, 2), 2))
    assert [value["raw_close"].item() for value in values] == [10., 20.]


def test_pool_history_keeps_writable_repository_compatibility(tmp_path, monkeypatch):
    from datetime import date

    from app.services.screener import ScreenerService

    service = ScreenerService(SimpleNamespace(store=SimpleNamespace(
        data_dir=tmp_path, read_only=False)))
    expected = pl.DataFrame({"symbol": ["600000.SH"]})
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: expected)
    assert service._load_pool_history(date(2026, 11, 2), 2).equals(expected)


def test_pool_history_uses_existing_prepared_repository_cache(tmp_path, monkeypatch):
    from datetime import date

    from app.services.screener import ScreenerService

    cached = pl.DataFrame({"symbol": ["600000.SH"], "raw_close": [10.]})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path, read_only=True),
                           get_enriched_history=lambda *_: cached)
    service = ScreenerService(repo)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: cached)
    assert service._load_pool_history(date(2026, 11, 2), 2).equals(cached)


def test_monitor_cold_start_does_not_import_backtest_engine(tmp_path, monkeypatch):
    import builtins
    from pathlib import Path

    from app.config import settings
    from app.multitenancy.compute_worker import execute
    from app.multitenancy.strategies import (
        definition_payload,  # noqa: F401 -- import before engine stub
    )

    monkeypatch.setattr(settings, "data_dir", settings.data_dir)
    monkeypatch.setattr("app.strategy.engine.StrategyEngine", lambda *_args, **_kwargs:
                        SimpleNamespace(load_errors=lambda: []))
    monkeypatch.setattr("app.tickflow.repository.DataStore", lambda *_args, **_kwargs:
                        SimpleNamespace(db=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr("app.tickflow.repository.KlineRepository", lambda _: None)
    monkeypatch.setattr("app.multitenancy.monitor_compute.evaluate_snapshot", lambda *_: {"events": []})
    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path:
                        iter(()) if str(path) == "/market" else original_iterdir(path))
    original_import = builtins.__import__

    def checked_import(name, *args, **kwargs):
        assert not name.startswith("app.backtest"), "monitor cold start loaded research modules"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", checked_import)
    assert execute({"kind": "monitor"}, tmp_path) == {"events": []}


def test_failed_minute_write_never_notifies_publisher(tmp_path, monkeypatch):
    calls = []
    service = MinuteRefreshService(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))
    service.set_app_state(SimpleNamespace(public_publisher=SimpleNamespace(
        request_minute_publish=lambda at: calls.append(at))))
    monkeypatch.setattr(service, "_resolve_custom", lambda: (None, "tickflow"))
    monkeypatch.setattr(service, "_select_mode", lambda: "increment")
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_universe_increment",
                        lambda: (pl.DataFrame({"symbol": ["600000.SH"]}), 1))
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition",
                        lambda *_: (_ for _ in ()).throw(OSError("write failed")))
    with pytest.raises(OSError, match="write failed"):
        service._run_round()
    assert calls == [] and service._state.rounds == 0


def test_release_metadata_uses_completed_round_before_copy(tmp_path, monkeypatch):
    from app.market_time import CN_TZ

    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    at = datetime(2026, 11, 4, 14, 31, 28, tzinfo=CN_TZ)
    publisher.request_minute_publish(at)
    monkeypatch.setattr(publisher, "validate_daily_coverage", lambda: None)
    monkeypatch.setattr(publisher, "generations", lambda: {})
    monkeypatch.setattr(publisher, "inventory", lambda: {})
    # A subsequent notification during copying belongs to the next release.
    original = publisher._swap_current

    def swap(link):
        publisher.request_minute_publish(at.replace(second=29))
        original(link)

    monkeypatch.setattr(publisher, "_swap_current", swap)
    manifest = publisher.publish()
    assert manifest["minute_completed_at"] == at.timestamp()
    assert publisher.changed.is_set()


def test_priority_waiter_overtakes_background_without_leaking_permits():
    import threading

    waiting = threading.Event()
    order = []

    def status():
        if threading.current_thread().name == "priority":
            waiting.set()
        return budget_status(8192, 4, 1)

    permits = BudgetPermits(status)
    assert permits.acquire(blocking=False)

    def run(priority):
        assert permits.acquire(timeout=2, priority=priority)
        order.append(priority)
        permits.release()

    background = threading.Thread(target=run, args=(False,))
    priority = threading.Thread(target=run, args=(True,), name="priority")
    background.start()
    priority.start()
    assert waiting.wait(1)
    permits.release()
    background.join(3)
    priority.join(3)
    assert order == [True, False]
    assert permits.usage()["active"] == 0 and permits.priority_waiters == 0


def test_publication_bursts_are_coalesced_to_five_second_spacing(tmp_path, monkeypatch):
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    clock, attempts = {"now": 100., "waits": 0}, []

    class Changed:
        def wait(self, _seconds):
            clock["waits"] += 1
            if clock["waits"] == 3:
                publisher.stopped.set()
        def clear(self):
            pass

    def pacing(seconds):
        clock["now"] += seconds
        return publisher.stopped.is_set()

    publisher.changed = Changed()
    monkeypatch.setattr(publisher.stopped, "wait", pacing)
    monkeypatch.setattr("app.multitenancy.publication.time.monotonic", lambda: clock["now"])
    monkeypatch.setattr(publisher, "publish", lambda: attempts.append(clock["now"]))
    publisher._loop()
    assert attempts == [100., 105.]


def test_publication_capacity_backoff_cannot_be_bypassed_by_minute_events(tmp_path, monkeypatch):
    import errno

    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    waits = []
    monkeypatch.setattr(publisher.changed, "wait", lambda _: True)
    monkeypatch.setattr("app.multitenancy.publication.time.monotonic", lambda: 100.)

    def wait(seconds):
        waits.append(seconds)
        return seconds == 300

    monkeypatch.setattr(publisher.stopped, "wait", wait)
    monkeypatch.setattr(publisher, "publish",
                        lambda: (_ for _ in ()).throw(OSError(errno.ENOSPC, "capacity reserve")))
    publisher._loop()
    assert waits == [0, 300]


@pytest.mark.parametrize("code,seconds", [(409, 1), (429, 1), (403, 5), (503, 5)])
def test_realtime_retry_only_accelerates_conflicts_and_busy(code, seconds):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC

    from app.cell.context import AccessError
    from app.multitenancy.runtime import FullWorker

    worker = object.__new__(FullWorker)
    due = []
    worker.scheduler = SimpleNamespace(
        claim=lambda _: [{"tenant_id": "test"}], finish=lambda _claim, at: due.append(at))
    worker.market = SimpleNamespace(snapshot=lambda: {"status": {"is_trading_hours": True}})
    worker.db = SimpleNamespace(settings=SimpleNamespace(realtime_interval=1))
    worker.evaluate = lambda *_: (_ for _ in ()).throw(AccessError(code, "deferred"))
    before = datetime.now(UTC)
    with ThreadPoolExecutor(max_workers=1) as worker.pool:
        assert worker.tick() == 1
    assert seconds <= (due[0] - before).total_seconds() < seconds + .5
