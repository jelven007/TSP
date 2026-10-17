"""Public publication, platform authorization and live-read bridging."""
import json
import time
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.enriched_generation import bump_enriched_generation, get_enriched_generation
from app.multitenancy.market import encode_frame
from app.multitenancy.platform import (
    management_path,
    market_read_access,
    market_read_path,
    platform_access,
)
from app.multitenancy.publication import PublicPublisher, release_path
from app.multitenancy.quote_bridge import PublicQuoteBridge


def market_files(source):
    source.mkdir()
    for asset in ("stock", "etf", "index"):
        get_enriched_generation(source, asset)
    path = source / "instruments" / "instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"], "name": ["公开数据"]}).write_parquet(path)
    private = source / "user_data"
    private.mkdir()
    (private / "secrets.json").write_text('{"password":"excluded"}')
    (source / ".env").write_text("excluded")
    ext = source / "ext_data" / "concept"
    ext.mkdir(parents=True)
    (ext / "config.json").write_text(json.dumps({
        "id": "concept", "name": "概念", "columns": [],
        "pull": {"headers": {"Authorization": "excluded"}},
    }))
    return path


def test_publication_copies_source_and_reuses_only_immutable_files(tmp_path):
    source, target = tmp_path / "source", tmp_path / "public"
    path = market_files(source)
    status = source / "instrument_status"
    status.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"], "is_st": [False]}).write_parquet(status / "history.parquet")
    releases = source / "data_releases"
    releases.mkdir()
    (releases / "current.json").write_text('{"release_id":"source-release"}')
    (source / "capabilities.json").write_text('{"schema_version":2,"capabilities":{}}')
    publisher = PublicPublisher(source, target)
    first = publisher.publish()
    release = release_path(target)
    assert release == release_path(target, first["id"])
    assert pl.read_parquet(release / "instrument_status/history.parquet")["is_st"].to_list() == [False]
    assert (release / "data_releases/current.json").read_bytes() == (releases / "current.json").read_bytes()
    assert (release / "capabilities.json").exists()
    assert not (release / "user_data").exists() and not (release / ".env").exists()
    assert "excluded" not in (release / "ext_data/concept/config.json").read_text()
    import hashlib
    assert set(first["checksums"]) == set(first["files"])
    for name, digest in first["checksums"].items():
        assert hashlib.sha256((release / name).read_bytes()).hexdigest() == digest
    assert (release / "instruments/instruments.parquet").stat().st_ino != path.stat().st_ino
    assert publisher.publish()["id"] == first["id"]
    # An in-place source write cannot mutate a published release.
    pl.DataFrame({"symbol": ["000001.SZ"], "name": ["新版本"]}).write_parquet(path)
    second = publisher.publish()
    assert second["id"] != first["id"]
    assert second["checksums"]["instruments/instruments.parquet"] != first["checksums"]["instruments/instruments.parquet"]
    assert pl.read_parquet(release / "instruments/instruments.parquet")["symbol"].to_list() == ["600000.SH"]
    assert (release / "ext_data/concept/config.json").stat().st_ino == (
        release_path(target) / "ext_data/concept/config.json"
    ).stat().st_ino
    for invalid in ("../source", "a" * 31, "/" * 32):
        with pytest.raises(ValueError):
            release_path(target, invalid)


def test_publication_rejects_a_changed_generation_and_keeps_previous(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "public"
    path = market_files(source)
    publisher = PublicPublisher(source, target)
    first = publisher.publish()
    path.touch()
    import shutil

    copy = shutil.copy2
    changed = False

    def concurrent_write(*args, **kwargs):
        nonlocal changed
        result = copy(*args, **kwargs)
        if not changed:
            changed = True
            bump_enriched_generation(source)
        return result

    monkeypatch.setattr(shutil, "copy2", concurrent_write)
    with pytest.raises(ValueError, match="changed"):
        publisher.publish()
    assert release_path(target).name == first["id"]
    assert len(list((target / "releases").iterdir())) == 1


def test_platform_only_accepts_explicit_management_paths(monkeypatch):
    monkeypatch.setenv("TSP_PLATFORM_ADMIN_TOKEN", "private-internal-token")
    for method, path in (
        ("GET", "/api/settings"), ("POST", "/api/pipeline/run"),
        ("POST", "/api/kline/rebuild_enriched"), ("POST", "/api/ext-data/concept/pull/run"),
        ("GET", "/api/ext-data"), ("HEAD", "/api/ext-data"),
    ):
        assert management_path(method, path)
        request = SimpleNamespace(method=method, url=SimpleNamespace(path=path),
                                  headers={"authorization": "Bearer private-internal-token"})
        assert platform_access(request)
        request.headers["authorization"] = "Bearer invalid"
        assert not platform_access(request)
    for method, path in (
        ("GET", "/api/watchlist"), ("POST", "/api/kline/daily-batch"),
        ("GET", "/api/settings/api-tokens"), ("POST", "/api/auth/login"),
        ("GET", "/api/strategies"), ("GET", "/api/data-other"),
        ("GET", "/api/ext-data/concept/rows"), ("GET", "/api/ext-data/schema-all"),
    ):
        assert not management_path(method, path)


def test_public_market_read_model_has_an_explicit_pure_read_allowlist(monkeypatch):
    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "market-read-token")
    for path in (
        "/api/intraday/status",
        "/api/overview/market",
        "/api/abnormal/overview",
        "/api/screener/market-snapshot",
        "/api/screener/limit-ladder",
        "/api/regime/mainline",
        "/api/sector-rotation",
    ):
        assert market_read_path("GET", path)
        request = SimpleNamespace(
            method="GET",
            url=SimpleNamespace(path=path),
            headers={"authorization": "Bearer market-read-token"},
        )
        assert market_read_access(request)
    for path in ("/api/watchlist", "/api/strategies", "/api/paper/accounts", "/api/events"):
        assert not market_read_path("GET", path)


def test_quote_bridge_keeps_depth_age_and_replaces_repository_on_new_release(tmp_path, monkeypatch):
    from app.config import settings
    from app.services.sector_monitor import SectorMonitorService
    from app.tickflow.repository import DataStore, KlineRepository

    source, target = tmp_path / "source", tmp_path / "public"
    path = market_files(source)
    publisher = PublicPublisher(source, target)
    first = publisher.publish()
    monkeypatch.setattr(settings, "data_dir", target / "current")
    store = DataStore(release_path(target), read_only=True)
    repo = KlineRepository(store)
    state = SimpleNamespace(repo=repo, sector_monitor_service=SectorMonitorService(repo))
    bridge = PublicQuoteBridge(repo, SimpleNamespace(close=lambda: None), state)
    frame = encode_frame(pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 10, 14)], "close": [10.0]}))
    snapshot = {
        "sequence": 1, "status": {"symbol_count": 1, "quote_age_ms": 10, "is_trading_hours": True},
        "frames": {"stock": frame, "etf": frame, "index": frame},
        "indices": encode_frame(pl.DataFrame()), "public_release": {"id": first["id"]},
        "depth": {"day": "2026-10-14", "ready": True, "age": 3,
                  "entries": {"600000.SH": {"sealed_up": True, "bid1_vol": 123}}},
    }
    bridge.update(snapshot)
    old = state.repo
    assert state.indicators_ready
    sealed = bridge.depth.get_sealed_map(date(2026, 10, 14), False)["600000.SH"]
    assert sealed["vol"] == 123 and 3 <= sealed["age"] < 4
    pl.DataFrame({"symbol": ["000001.SZ"], "name": ["新版本"]}).write_parquet(path)
    second = publisher.publish()
    bridge.update({**snapshot, "public_release": {"id": second["id"]}})
    assert state.repo is not old and state.repo.store.data_dir.name == second["id"]
    assert old.store.data_dir.name == first["id"]
    assert state.repo.get_instruments()["symbol"].to_list() == ["000001.SZ"]
    bridge.received_at = time.monotonic() - 20
    assert not bridge.status()["is_trading_hours"]
    bridge.close()
    store.db.close()


def test_api_quote_bridge_uses_markers_and_refreshes_frames_only_on_demand(tmp_path):
    from app.tickflow.repository import DataStore, KlineRepository

    store = DataStore(tmp_path, read_only=True)
    repo = KlineRepository(store)
    frame = encode_frame(pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2026, 10, 16)],
        "close": [10.0],
    }))
    snapshot = {
        "sequence": 1,
        "captured_at": time.time(),
        "status": {"symbol_count": 1, "quote_age_ms": 10, "is_trading_hours": True},
        "frames": {"stock": frame, "etf": frame, "index": frame},
        "indices": encode_frame(pl.DataFrame()),
    }

    class Market:
        calls = 0

        def snapshot(self):
            self.calls += 1
            return snapshot

        def close(self):
            pass

    class Markers:
        started = False

        def start(self):
            self.started = True

        def close(self):
            pass

        def status(self):
            return {"connected": True}

    market, markers = Market(), Markers()
    bridge = PublicQuoteBridge(repo, market)
    bridge.attach_markers(markers)
    try:
        bridge.start()
        assert markers.started and market.calls == 0
        bridge.update(snapshot)
        assert bridge.frame_decodes == 4
        bridge.mark_event("quotes_updated")
        bridge.ensure_fresh()
        assert market.calls == 1 and bridge.frame_decodes == 8
        bridge.ensure_fresh()
        assert market.calls == 1 and bridge.frame_decodes == 8
    finally:
        bridge.close()
        store.db.close()


def test_platform_quote_evaluation_never_reads_old_private_signals(monkeypatch):
    from app.config import settings
    from app.indicators.pipeline import _get_custom_signal_exprs, _get_custom_signal_exprs_today
    from app.services.quote_service import QuoteService
    from app.strategy import custom_signals

    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr(custom_signals, "load_all", lambda *a: pytest.fail("legacy signals read"))
    monkeypatch.setattr(custom_signals, "load_intraday_all", lambda *a: pytest.fail("legacy intraday signals read"))
    assert _get_custom_signal_exprs() == {} and _get_custom_signal_exprs_today() == {}
    quotes = QuoteService()
    assert quotes._load_intraday_signal_defs() == []
    quotes._evaluate_monitors(pl.DataFrame(), None)


def test_platform_public_reads_coalesce_concurrent_gets_and_preserve_post_bodies():
    import asyncio

    import httpx
    from fastapi import FastAPI, Request

    from app.multitenancy.read_cache import PublicReadCache

    app, cache, calls = FastAPI(), PublicReadCache(), []
    app.middleware("http")(cache.read)

    @app.get("/api/kline/minute")
    async def minute(day: str):
        calls.append(day)
        await asyncio.sleep(.01)
        return {"day": day}

    @app.post("/api/kline/daily-batch")
    async def batch(request: Request):
        body = await request.json()
        calls.append(body)
        return body

    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://platform.test") as client:
            results = await asyncio.gather(*(
                client.get("/api/kline/minute", params={"day": "2026-10-14"}) for _ in range(25)
            ))
            assert all(response.json() == {"day": "2026-10-14"} for response in results)
            assert calls == ["2026-10-14"]
            for _ in range(2):
                response = await client.post("/api/kline/daily-batch", json={"symbols": ["600000.SH"]})
                assert response.json() == {"symbols": ["600000.SH"]}
            assert len(calls) == 2
            # A different POST body must never reuse another response.
            assert (await client.post("/api/kline/daily-batch", json={"symbols": ["000001.SZ"]})).json() == {
                "symbols": ["000001.SZ"],
            }
            assert len(calls) == 3
    asyncio.run(check())


def test_platform_full_snapshot_serializes_cache_fill():
    from concurrent.futures import ThreadPoolExecutor

    from app.cell.platform_market import _serialize_full_snapshot

    cached = None
    calls = 0

    @_serialize_full_snapshot
    def snapshot():
        nonlocal cached, calls
        if cached is not None:
            return cached
        calls += 1
        time.sleep(0.05)
        cached = {"sequence": 1}
        return cached

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: snapshot(), range(40)))

    assert calls == 1
    assert results == [{"sequence": 1}] * 40


def test_platform_marker_stream_never_sends_market_frames(monkeypatch):
    import asyncio

    from app.cell.platform_market import market_events
    from app.services.quote_service import QuoteService

    monkeypatch.setenv("TSP_PLATFORM_MARKET_TOKEN", "market-read-token")
    quotes = QuoteService()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(quote_service=quotes)))

    async def check():
        response = await market_events(request, authorization="Bearer market-read-token")
        pending = asyncio.create_task(response.body_iterator.__anext__())
        await asyncio.sleep(0)
        quotes._broadcast_quote_updated()
        event = await asyncio.wait_for(pending, timeout=2)
        assert event["event"] == "quotes_updated"
        assert "frames" not in event["data"]
        await response.body_iterator.aclose()

    asyncio.run(check())
    assert not quotes._subscribers


@pytest.mark.parametrize("role", ["api", "gateway"])
def test_stateless_service_import_never_creates_legacy_files(tmp_path, role):
    import os
    import subprocess
    import sys
    from pathlib import Path

    public = tmp_path / "unavailable-public-release"
    result = subprocess.run(
        [sys.executable, "-c", """
import app.main
from app.services import auth, pipeline_jobs
assert pipeline_jobs.job_store is None
assert auth._sessions == {}
try:
    auth._path()
except RuntimeError:
    pass
else:
    raise AssertionError("legacy identity files remain accessible")
"""],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "SERVICE_ROLE": role, "MULTITENANCY_ENABLED": "true",
             "DATA_DIR": str(public), "NUMBA_CACHE_DIR": str(tmp_path / "numba"),
             "PYTHONDONTWRITEBYTECODE": "1"},
        text=True, capture_output=True, timeout=40,
    )
    assert result.returncode == 0, result.stderr
    assert not public.exists()


@pytest.mark.parametrize("role", ["api", "platform"])
def test_non_standalone_auth_never_restores_legacy_sessions(monkeypatch, role):
    from app.config import settings
    from app.services import auth

    monkeypatch.setattr(settings, "service_role", role)
    monkeypatch.setattr(auth, "_load", lambda: pytest.fail("legacy identity read"))
    auth._restore_sessions()
    with pytest.raises(RuntimeError, match="legacy identity"):
        auth._path()
