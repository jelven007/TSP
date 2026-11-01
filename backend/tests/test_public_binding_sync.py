"""Idle publication synchronization, atomic readiness and reader lifetimes."""
from __future__ import annotations

import gc
import os
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.config import settings
from app.enriched_generation import get_enriched_generation
from app.multitenancy.market import encode_frame
from app.multitenancy.publication import PublicPublisher, release_path
from app.multitenancy.quote_bridge import PublicQuoteBridge
from app.tickflow.repository import DataStore, KlineRepository
from scripts.operations.maintain_full_host import release_candidates


@pytest.fixture
def public_api(tmp_path, monkeypatch):
    source, public = tmp_path / "source", tmp_path / "public"
    instruments = source / "instruments" / "instruments.parquet"
    instruments.parent.mkdir(parents=True)
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["公开数据"]}).write_parquet(instruments)
    publisher = PublicPublisher(source, public)
    first = publisher.publish()
    monkeypatch.setattr(settings, "data_dir", public / "current")
    frame = encode_frame(pl.DataFrame({
        "symbol": ["600000.SH"], "date": [date(2026, 10, 21)], "close": [10.0],
    }))
    fetched = threading.Event()

    class Market:
        calls = 0
        payload = None

        def snapshot(self):
            self.calls += 1
            fetched.set()
            return self.payload or {
                "sequence": self.calls, "captured_at": time.time(), "platform_ready": True,
                "status": {"symbol_count": 1}, "frames": {"stock": frame, "index": frame},
                "indices": encode_frame(pl.DataFrame()),
                "public_release": {"id": release_path(public).name},
                "depth": {"entries": {}, "ready": True, "day": "2026-10-21", "age": 1},
            }

        def close(self):
            pass

    @contextmanager
    def connection():
        workers = [
            {"kind": kind, "healthy": True, "age_seconds": 1}
            for kind in ("research", "realtime", "notifications", "scheduled")
        ]
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(
            fetchall=lambda: workers, fetchone=lambda: {"value": 1},
        ))

    db = SimpleNamespace(
        settings=SimpleNamespace(id="test"),
        control=SimpleNamespace(connection=connection),
        data=SimpleNamespace(connection=connection),
    )
    app = FastAPI()
    app.include_router(routes.router)
    app.state.account_identity = SimpleNamespace(db=db)
    app.state.compute_executor = SimpleNamespace(health=lambda: {"ready": True})
    monkeypatch.setattr(routes, "settings", SimpleNamespace(
        multitenancy_enabled=True, service_role="api", data_dir=public / "current",
    ))
    initial = DataStore(release_path(public), read_only=True)
    market = Market()
    bridge = PublicQuoteBridge(KlineRepository(initial), market, app.state)
    app.state.quote_service, app.state.depth_service = bridge, bridge.depth
    bridge.refresh()
    bridge.attach_markers(SimpleNamespace(
        start=lambda: None, close=lambda: None, status=lambda: {"connected": True},
    ))
    fetched.clear()

    def publish():
        pl.DataFrame({"symbol": ["000001.SZ"], "name": [str(time.time_ns())]}).write_parquet(instruments)
        return publisher.publish()

    api = SimpleNamespace(
        app=app, bridge=bridge, market=market, publish=publish, public=public,
        first=first, initial=initial, fetched=fetched, client=TestClient(app),
    )
    del initial
    yield api
    bridge.close()
    if api.initial is not None:
        api.initial.db.close()


@pytest.mark.parametrize("attempt", range(3))
def test_idle_reader_health_survives_normal_release_retention(public_api, attempt):
    api = public_api
    assert api.client.get("/health/ready").status_code == 200
    old = release_path(api.public)
    # Exercise real publication, retention and HTTP routing, with no request
    # between publication and the observation of the automatic commit.
    api.publish()
    latest = api.publish()
    past = time.time() - 9 * 3600
    os.utime(old, (past, past))
    assert release_candidates(api.public, protected=set(), keep=2, min_age_hours=6) == [old]
    if hasattr(api.bridge, "binding_sync"):
        api.bridge.binding_sync.interval = .01
    api.bridge.start()
    assert api.fetched.wait(1), "idle API never fetched the changed public release"
    with api.bridge.refresh_lock:
        assert api.app.state.repo.store.data_dir.name == latest["id"]
    from scripts.operations.maintain_full_host import delete_release_if_unused

    api.initial = None
    gc.collect()
    assert delete_release_if_unused(
        old, root=api.public, protected=set(), read_references=set, keep=2, min_age_hours=6,
    ) == "deleted"
    assert api.client.get("/health/ready").status_code == 200


def test_unchanged_release_does_not_fetch_on_markers(public_api):
    bridge = public_api.bridge
    assert hasattr(bridge, "binding_sync"), "publication-only synchronizer is missing"
    calls, decodes = public_api.market.calls, bridge.frame_decodes
    for _ in range(12):
        bridge.mark_event("quotes_updated")
        bridge.binding_sync.check_once()
    assert public_api.market.calls == calls
    assert bridge.frame_decodes == decodes


def test_same_release_business_refresh_reuses_repository(public_api):
    api = public_api
    repo = api.app.state.repo
    api.bridge.mark_event("quotes_updated")
    api.bridge.ensure_fresh()
    assert api.app.state.repo is repo
    assert api.market.calls == 2


def test_busy_refresh_preserves_mismatch_age_and_fails_closed(public_api):
    api = public_api
    assert hasattr(api.bridge, "binding_sync"), "publication-only synchronizer is missing"
    sync = api.bridge.binding_sync
    clock = [100.]
    sync.clock = lambda: clock[0]
    sync.check_once()
    api.publish()
    old = api.app.state.repo
    with api.bridge.refresh_lock:
        sync.check_once()
        assert api.app.state.repo is old
        assert api.client.get("/health/ready").status_code == 200
        clock[0] += 31
        sync.check_once()
        response = api.client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["public_files"]["last_error_kind"] == "sync_stalled"
    sync.check_once()
    assert api.client.get("/health/ready").status_code == 200


def test_slow_prepare_does_not_block_readiness_and_rejects_obsolete_candidate(public_api):
    api = public_api
    assert hasattr(api.bridge, "binding_sync"), "publication-only synchronizer is missing"
    api.bridge.binding_sync.check_once()
    old = api.app.state.repo
    api.publish()
    entered, proceed = threading.Event(), threading.Event()
    snapshot = api.market.snapshot

    def blocked():
        value = snapshot()
        entered.set()
        assert proceed.wait(3)
        return value

    api.market.snapshot = blocked
    thread = threading.Thread(target=api.bridge.binding_sync.check_once)
    thread.start()
    try:
        assert entered.wait(1)
        before = time.monotonic()
        response = api.client.get("/health/ready")
        assert response.status_code == 200
        assert time.monotonic() - before < .5
        latest = api.publish()
    finally:
        proceed.set()
        thread.join(3)
    assert not thread.is_alive()
    assert api.app.state.repo is old
    api.market.snapshot = snapshot
    api.bridge.binding_sync.check_once()
    assert api.app.state.repo.store.data_dir.name == latest["id"]


def test_failed_prepare_preserves_all_committed_references(public_api, monkeypatch, caplog):
    from app.services import sector_monitor

    api = public_api
    assert hasattr(api.bridge, "binding_snapshot"), "consistent binding view is missing"
    previous = api.bridge.binding_snapshot()
    sector = api.app.state.sector_monitor_service
    depth = api.bridge.depth
    api.publish()

    def broken(repo):
        raise ValueError("private error detail must not escape")

    monkeypatch.setattr(sector_monitor, "SectorMonitorService", broken)
    api.bridge.binding_sync.check_once()
    current = api.bridge.binding_snapshot()
    assert current.repo is previous.repo and current.snapshot is previous.snapshot
    assert api.app.state.sector_monitor_service is sector
    assert api.bridge.depth is depth
    assert current.sync["last_error_kind"] == "prepare_failed"
    assert api.client.get("/health/ready").status_code == 200
    assert "kind=prepare_failed" in caplog.text
    assert "private error detail" not in caplog.text


def test_retired_store_remains_open_until_last_inflight_reference(public_api):
    api = public_api
    assert hasattr(api.bridge, "binding_snapshot"), "consistent binding view is missing"
    old = api.bridge.binding_snapshot()
    store_ref = weakref.ref(old.repo.store)
    connection = old.repo.store.db
    api.publish()
    api.bridge.binding_sync.check_once()
    assert connection.execute("SELECT 1").fetchone() == (1,)
    assert old.lease.held
    # The fixture's initial repo is reused on the initial refresh; release its
    # explicit reference to model lifespan's removal of startup locals.
    api.initial = None
    del old
    gc.collect()
    assert store_ref() is None
    with pytest.raises(Exception, match="closed"):
        connection.execute("SELECT 1")


def test_sync_stale_current_invalid_and_shutdown_fail_closed(public_api):
    api = public_api
    assert hasattr(api.bridge, "binding_sync"), "publication-only synchronizer is missing"
    sync = api.bridge.binding_sync
    clock = [100.]
    sync.clock = lambda: clock[0]
    sync.check_once()
    clock[0] += 31
    assert api.client.get("/health/ready").status_code == 503
    sync.check_once()
    assert api.client.get("/health/ready").status_code == 200
    (api.public / "current").unlink()
    assert api.client.get("/health/ready").status_code == 503
    sync.close()
    assert sync.snapshot()["last_error_kind"] == "stopped"


def test_stop_during_prepare_never_installs_candidate(public_api):
    api = public_api
    assert hasattr(api.bridge, "binding_sync"), "publication-only synchronizer is missing"
    sync = api.bridge.binding_sync
    previous = api.app.state.repo
    api.publish()
    entered, proceed = threading.Event(), threading.Event()
    snapshot = api.market.snapshot

    def blocked():
        entered.set()
        assert proceed.wait(3)
        return snapshot()

    api.market.snapshot = blocked
    thread = threading.Thread(target=sync.check_once)
    thread.start()
    try:
        assert entered.wait(1)
        sync.close(timeout=0)
    finally:
        proceed.set()
        thread.join(3)
    assert not thread.is_alive() and api.app.state.repo is previous
    assert api.client.get("/health/ready").status_code == 503


def test_over_budget_candidate_is_discarded_and_reports_stall(public_api, monkeypatch):
    from app.multitenancy import quote_bridge

    api = public_api
    sync = api.bridge.binding_sync
    now = [100.]
    sync.clock = lambda: now[0]
    sync.check_once()
    previous = api.bridge.binding_snapshot()
    api.publish()
    open_repo = quote_bridge.open_public_repository
    candidate = []

    def slow_open(path):
        repo = open_repo(path)
        candidate.append(repo.store.db)
        now[0] += 31
        return repo

    monkeypatch.setattr(quote_bridge, "open_public_repository", slow_open)
    sync.check_once()
    assert api.bridge.binding_snapshot().repo is previous.repo
    assert api.client.get("/health/ready").status_code == 503
    assert sync.snapshot()["last_error_kind"] == "sync_stalled"
    with pytest.raises(Exception, match="closed"):
        candidate[0].execute("SELECT 1")


def test_shutdown_timeout_keeps_active_resources_and_rejects_late_commit(public_api, caplog):
    api = public_api
    sync = api.bridge.binding_sync
    previous = api.bridge.binding_snapshot()
    api.publish()
    entered, proceed = threading.Event(), threading.Event()
    snapshot = api.market.snapshot

    def blocked():
        entered.set()
        assert proceed.wait(3)
        return snapshot()

    api.market.snapshot = blocked
    sync.start()
    try:
        assert entered.wait(1)
        sync.close(timeout=.01)
        assert sync.thread.is_alive()
        assert "shutdown kind=sync_stalled" in caplog.text
        assert previous.lease.held
        assert previous.repo.db.execute("SELECT 1").fetchone() == (1,)
        assert api.client.get("/health/ready").status_code == 503
    finally:
        proceed.set()
        sync.close(timeout=3)
    assert not sync.thread.is_alive()
    assert api.bridge.binding_snapshot().repo is previous.repo


def _idle_process(public, initial_path, template, results, stopped):
    """Each spawned process owns its bridge, thread, repository and lease."""
    from pathlib import Path

    settings.data_dir = Path(public) / "current"
    fetched = threading.Event()

    def snapshot():
        fetched.set()
        return {**template, "public_release": {"id": release_path(Path(public)).name}}

    initial = DataStore(Path(initial_path), read_only=True)
    bridge = PublicQuoteBridge(KlineRepository(initial), SimpleNamespace(snapshot=snapshot, close=lambda: None))
    try:
        bridge.refresh()
        fetched.clear()
        bridge.binding_sync.interval = .01
        results.put("ready")
        bridge.start()
        assert fetched.wait(10)
        with bridge.refresh_lock:
            results.put(bridge.bound_release().name)
        stopped.wait(10)
    finally:
        bridge.close()
        initial.db.close()


def test_four_processes_follow_current_without_any_http_requests(public_api):
    import multiprocessing

    api = public_api
    context = multiprocessing.get_context("spawn")
    results, stopped = context.Queue(), context.Event()
    processes = [
        context.Process(target=_idle_process, args=(
            str(api.public), str(release_path(api.public)), api.bridge.snapshot, results, stopped,
        )) for _ in range(4)
    ]
    try:
        for process in processes:
            process.start()
        assert [results.get(timeout=15) for _ in processes] == ["ready"] * 4
        latest = api.publish()
        assert [results.get(timeout=15) for _ in processes] == [latest["id"]] * 4
    finally:
        stopped.set()
        for process in processes:
            process.join(10)
            if process.is_alive():
                process.terminate()
                process.join(5)
        results.close()
    assert all(process.exitcode == 0 for process in processes)


def test_repeat_start_and_close_do_not_duplicate_threads(public_api):
    api = public_api
    bridge = api.bridge
    starts = []
    bridge.markers.start = lambda: starts.append(True)
    bridge.start()
    thread = bridge.binding_sync.thread
    bridge.start()
    assert bridge.binding_sync.thread is thread
    bridge.close()
    bridge.close()
    bridge.start()
    assert not thread.is_alive() and starts == [True]


def test_broadcast_failure_does_not_close_committed_store(public_api, monkeypatch):
    api = public_api
    latest = api.publish()
    monkeypatch.setattr(api.bridge, "_broadcast_quote_updated", lambda: (_ for _ in ()).throw(
        RuntimeError("private notification detail"),
    ))
    api.bridge.binding_sync.check_once()
    view = api.bridge.binding_snapshot()
    assert view.repo.store.data_dir.name == latest["id"] and view.lease.held
    assert view.repo.db.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.asyncio
async def test_lifespan_uses_committed_sector_and_releases_initial_store(public_api, monkeypatch):
    from pydantic import SecretStr

    from app.data_providers import custom
    from app.multitenancy import (
        events,
        executor,
        lifespan,
        market,
        platform,
        public_reader,
        quote_markers,
    )
    from app.strategy import engine
    from app.tickflow import policy

    api = public_api
    db = api.app.state.account_identity.db
    db.settings = SimpleNamespace(
        id="test", deployment_exposure="local", bootstrap_admin_password=SecretStr(""),
        stream_ticket_secret="test-stream-secret-" * 2, market_url="http://test",
        market_token=SecretStr("test-token"), schema_version=6,
    )
    db.open, db.close = lambda: None, lambda: None
    monkeypatch.setattr(settings, "service_role", "api")
    monkeypatch.setattr(lifespan, "CellSettings", lambda: object())
    monkeypatch.setattr(lifespan, "Databases", lambda _: db)
    monkeypatch.setattr(lifespan, "bootstrap_full_identity", lambda *a, **kw: None)
    monkeypatch.setattr(lifespan, "AccountIdentity", lambda _: SimpleNamespace(
        set_registration_secret=lambda *a, **kw: None, recover_pending=lambda: None,
        bootstrap_admin_tenants=lambda: None, registration_enabled=lambda: True,
    ))
    monkeypatch.setattr(custom, "load_all", lambda: None)
    monkeypatch.setattr(custom, "close_all", lambda: None)
    monkeypatch.setattr(policy, "detect_capabilities", lambda: {})
    monkeypatch.setattr(events, "EventHub", lambda _: SimpleNamespace(start=lambda: None, close=lambda: None))
    monkeypatch.setattr(quote_markers, "QuoteMarkerBridge", lambda *a, **kw: SimpleNamespace(
        start=lambda: None, close=lambda: None, status=lambda: {"connected": True},
    ))
    monkeypatch.setattr(engine, "StrategyEngine", lambda **kw: object())
    monkeypatch.setattr(executor, "ComputeExecutor", lambda: object())

    class Admin:
        def __init__(self, *args):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(platform, "PlatformAdmin", Admin)
    opened = []
    open_repo = public_reader.open_public_repository

    def record_open(path):
        repo = open_repo(path)
        opened.append(weakref.ref(repo.store))
        return repo

    monkeypatch.setattr(public_reader, "open_public_repository", record_open)
    snapshot = api.market.snapshot
    published = []

    def startup_snapshot():
        if not published:
            published.append(api.publish())
        return snapshot()

    api.market.snapshot = startup_snapshot
    monkeypatch.setattr(market, "PublicMarket", lambda _: api.market)
    app = FastAPI()
    async with lifespan.lifespan(app):
        quotes = app.state.quote_service
        assert quotes.binding_sync.thread.is_alive()
        assert app.state.repo.store.data_dir.name == published[0]["id"]
        assert app.state.sector_monitor_service._repo is app.state.repo
        gc.collect()
        assert opened[0]() is None, "startup locals retained the original public store"
    assert not quotes.binding_sync.thread.is_alive()
