"""Regressions for expensive status reads, transport retry and stream metrics."""
import asyncio
from datetime import datetime, timedelta

import polars as pl
import pytest

from app.api import data
from app.services import observability, suspension_sync

pytest_plugins = ("test_suspension_sync",)


def test_parquet_profile_reuses_exact_counts_and_invalidates_replacement(tmp_path, monkeypatch):
    root = tmp_path / "kline_minute"
    root.mkdir()
    path = root / "part.parquet"
    pl.DataFrame({"symbol": ["A", "A", "B"]}).write_parquet(path)
    original = pl.scan_parquet
    scans = []

    def scan(*args, **kwargs):
        scans.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(pl, "scan_parquet", scan)
    assert data._parquet_row_symbol_stats(root) == (3, 2)
    assert data._parquet_row_symbol_stats(root) == (3, 2)
    assert len(scans) == 1
    replacement = root / "replacement.tmp"
    pl.DataFrame({"symbol": ["C"]}).write_parquet(replacement)
    replacement.replace(path)
    assert data._parquet_row_symbol_stats(root) == (1, 1)
    assert len(scans) == 2
    path.unlink()
    assert data._parquet_row_symbol_stats(root) == (0, 0)


def test_network_retries_back_off_and_keep_partial_coverage(env, monkeypatch):
    original = env.lookup.market_notices

    def lookup(exchange, *args, **kwargs):
        if exchange == "SZ":
            raise TimeoutError("upstream timed out")
        return original(exchange, *args, **kwargs)

    monkeypatch.setattr(env.lookup, "market_notices", lookup)
    delays = []
    for _ in range(3):
        state = suspension_sync.request_sync(env.repo, lookup=env.lookup)
        retry = datetime.fromisoformat(state["next_retry_at"])
        delays.append((retry - env.clock[0]).total_seconds())
        assert suspension_sync.summary(env.repo)["covered_count"] == 2
        assert env.repo.get_suspension_history().height == 1
        env.clock[0] = retry
    assert delays == [60, 120, 240]


def test_semantic_notice_waits_but_new_announcements_continue(env, monkeypatch):
    from test_suspension_sync import notice

    checks = []
    original = env.lookup.verify_notice

    def verify(notice, **kwargs):
        checks.append(notice["symbol"])
        if notice["symbol"] == env.symbols[1]:
            return {"events": [], "diagnostics": ["正文未确认生效日"],
                    "retry_category": "semantic"}
        return original(notice, **kwargs)

    monkeypatch.setattr(env.lookup, "verify_notice", verify)
    suspension_sync.request_sync(env.repo, lookup=env.lookup)
    env.clock[0] += timedelta(seconds=60)
    suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert checks == [env.symbols[1]]
    env.clock[0] += timedelta(seconds=240)
    monkeypatch.setattr(env.lookup, "market_notices", lambda exchange, *a, **k: {
        "notices": [notice(), notice(env.symbols[0])] if exchange == "SH" else [],
        "pages": 1, "rows": 2,
    })
    state = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert checks == [env.symbols[1], env.symbols[0]]
    assert env.repo.get_suspension_history()["symbol"].to_list() == [env.symbols[0]]
    assert state["status"] == "failed" and len(state["pending"]) == 1


def test_profile_copy_is_rebound_and_publication_avoids_symbol_rescan(tmp_path, monkeypatch):
    import shutil

    from app.services.parquet_profile import prepare_profiles, rebind_profiles

    source, stage = tmp_path / "source", tmp_path / "stage"
    root = source / "kline_daily"
    root.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A", "B"]}).write_parquet(root / "part.parquet")
    prepare_profiles(source)
    shutil.copytree(source, stage)
    rebind_profiles(stage)
    monkeypatch.setattr(pl, "scan_parquet", lambda *a, **k: pytest.fail("copied profile must be reused"))
    assert data._parquet_row_symbol_stats(stage / "kline_daily") == (2, 2)


def test_profile_rejects_corruption_and_never_saves_racing_inventory(tmp_path, monkeypatch):
    from app.services import parquet_profile

    root = tmp_path / "daily"
    root.mkdir()
    pl.DataFrame({"symbol": ["A"]}).write_parquet(root / "part.parquet")
    (root / parquet_profile.PROFILE).write_text('{"schema":1,"symbols":[],"files":{}}')
    assert data._parquet_row_symbol_stats(root) == (1, 1)
    original = parquet_profile._inventory
    calls = [0]

    def inventory(path):
        calls[0] += 1
        result = original(path)
        if calls[0] == 2:
            result["part.parquet"][0] += 1
        return result

    before = (root / parquet_profile.PROFILE).read_bytes()
    (root / "part.parquet").touch()
    monkeypatch.setattr(parquet_profile, "_inventory", inventory)
    with pytest.raises(ValueError, match="changed while profiling"):
        data._parquet_row_symbol_stats(root)
    assert (root / parquet_profile.PROFILE).read_bytes() == before


def test_measurement_merge_rejects_mixed_http_populations():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts/performance/summarize_observability.py"
    spec = importlib.util.spec_from_file_location("p2_summary", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observability.reset_http_metrics()
    before = observability.http_metrics_snapshot()
    observability.observe_http_duration(method="GET", route="/ordinary", status_code=200, duration_ms=10)
    after = observability.http_metrics_snapshot()
    after["process"]["uptime_seconds"] = before["process"]["uptime_seconds"] + 1
    assert module.summarize([(before, after)])["requests"] == 1
    before["measurement"] = "http_completion_v1"
    with pytest.raises(ValueError, match="populations differ"):
        module.summarize([(before, after)])


def test_sse_lifetime_is_separate_and_disconnect_releases_gauges(monkeypatch):
    observability.reset_http_metrics()
    clock = [0.0]
    monkeypatch.setattr(observability.time, "perf_counter", lambda: clock[0])

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/event-stream; charset=utf-8")]})
        clock[0] = 0.025
        await send({"type": "http.response.body", "body": b"data: hello\n\n", "more_body": True})
        clock[0] = 60.0
        raise asyncio.CancelledError()

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        return None

    scope = {"type": "http", "method": "GET", "path": "/api/events", "headers": []}
    middleware = observability.ObservabilityMiddleware(app, router=None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware(scope, receive, send))
    result = observability.http_metrics_snapshot()
    assert result["totals"]["requests"] == 0
    assert result["in_flight"] == 0
    assert result["streams"]["in_flight"] == 0
    assert result["streams"]["totals"]["requests"] == 1
    assert result["streams"]["totals"]["duration_ms"]["sum"] == 60000
    assert result["streams"]["first_byte"]["totals"]["duration_ms"]["sum"] == 25
    observability.reset_http_metrics()
