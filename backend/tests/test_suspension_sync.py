"""New halt discovery is independent of missing daily prices and known halts."""
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.instrument_suspension import STATUS_COLUMN, dated_status, pool_evidence
from app.market_time import CN_TZ
from app.services import pipeline_jobs, suspension_sync
from app.tickflow.repository import DataStore, KlineRepository

SYMBOL = "600567.SH"
DAY = date(2026, 11, 19)
URL = "https://www.sse.com.cn/disclosure/notice.pdf"
REAL_PUBLISH = suspension_sync._publish


def notice(symbol=SYMBOL, label="停牌"):
    return {"symbol": symbol, "title": label + "公告", "formal_date": "2026-11-19",
            "announced_at": "2026-11-18T22:12:00", "url": URL + "?symbol=" + symbol}


def event(symbol=SYMBOL, suspended=True, stamp=None):
    stamp = stamp or datetime(2026, 11, 18, 22, 12)
    return {"symbol": symbol, "effective_date": DAY, "announced_on": stamp.date(),
            "announced_at": stamp, "is_suspended": suspended, "source_url": URL}


@pytest.fixture
def env(tmp_path, monkeypatch):
    symbols = ["600001.SH", SYMBOL, "000635.SZ"]
    path = tmp_path / "instruments/instruments.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": symbols, "name": symbols}).write_parquet(path)
    source = tmp_path / "kline_daily/date=2026-11-18/part.parquet"
    source.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": symbols, "date": [date(2026, 11, 18)] * 3,
                  "close": [10.] * 3, "volume": [100.] * 3}).write_parquet(source)
    repo = KlineRepository(DataStore(tmp_path))
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    clock = [datetime(2026, 11, 18, 23, tzinfo=CN_TZ)]
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(settings, "service_role", "standalone")
    monkeypatch.setattr(suspension_sync, "cn_now", lambda: clock[0])
    monkeypatch.setattr(suspension_sync.preferences, "get_data_sync_auto_enabled", lambda: True)
    published, calls = [], []
    monkeypatch.setattr(suspension_sync, "_publish",
                        lambda *a: published.append(repo.get_suspension_history().height) or
                        {"data_release_id": "release", "public_release_id": None})

    class Lookup:
        def market_notices(self, exchange, begin, end, **kwargs):
            calls.append(exchange)
            assert begin == clock[0].date() - timedelta(days=3)
            assert end == clock[0].date() + timedelta(days=1)
            return {"notices": [notice()] if exchange == "SH" else [], "pages": 1, "rows": 1}

        def verify_notice(self, item, *, cutoff):
            calls.append(item["symbol"])
            return {"events": [event(item["symbol"])], "diagnostics": []}

    yield SimpleNamespace(repo=repo, store=store, clock=clock, lookup=Lookup(), calls=calls,
                          published=published, symbols=symbols, source=source)
    repo.db.close()


def test_healthy_previous_daily_still_discovers_tomorrow_halt_and_cache_changes(env):
    from app.services.selection_suspension_repair import repair_suspensions

    before = env.source.read_bytes()
    assert env.repo.get_suspension_history().is_empty()
    old = repair_suspensions(env.repo, DAY - timedelta(days=1),
                             lookup=lambda *a, **k: pytest.fail("no daily gap"),
                             check_cancelled=lambda: None)
    assert old["checked_symbols"] == []
    state = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert state["status"] == "succeeded" and state["added"] == 1
    assert env.calls == ["SH", "SZ", SYMBOL]
    result = suspension_sync.summary(env.repo)
    assert result["universe_count"] == result["covered_count"] == 3
    assert result["coverage_complete"] and result["pending_count"] == 0
    assert env.published == [1] and before == env.source.read_bytes()
    assert dated_status(env.repo.get_suspension_history(), DAY - timedelta(days=1)).is_empty()
    evidence = pool_evidence(env.repo, DAY, pl.DataFrame({"symbol": env.symbols}),
                             observed_at=datetime(2026, 11, 19, 8))
    assert evidence.filter(pl.col(STATUS_COLUMN))["symbol"].to_list() == [SYMBOL]
    assert evidence.height == 3


def test_completed_sweep_survives_restart_then_discovers_fresh_events(env, monkeypatch):
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=env.store._store_dir))
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["job_id"] == first["job_id"]
    assert len(env.calls) == 3
    env.clock[0] += timedelta(minutes=5)
    suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert env.calls == ["SH", "SZ", SYMBOL, "SH", "SZ"] and env.published == [1]


def test_failed_market_preserves_valid_event_and_exact_coverage_then_resumes(env, monkeypatch):
    original = env.lookup.market_notices
    def feed(exchange, *args, **kwargs):
        if exchange == "SZ":
            raise RuntimeError("SZ unavailable")
        return original(exchange, *args)
    monkeypatch.setattr(env.lookup, "market_notices", feed)
    state = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert state["status"] == "failed" and state["errors"]["SZ"] == "SZ unavailable"
    assert env.repo.get_suspension_history().height == 1 and env.published == [1]
    coverage = suspension_sync.summary(env.repo)
    assert coverage["universe_count"] == 3 and coverage["covered_count"] == 2
    assert coverage["pending_symbols"] == ["000635.SZ"] and not coverage["coverage_complete"]
    env.clock[0] += timedelta(minutes=1)
    monkeypatch.setattr(env.lookup, "market_notices", original)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "succeeded"
    assert env.calls.count("SH") == 1


def test_attachment_failure_does_not_starve_other_events_or_new_feed(env, monkeypatch):
    original = env.lookup.verify_notice
    def verify(item, **kwargs):
        if item["symbol"] == SYMBOL:
            return {"events": [], "diagnostics": ["PDF unavailable"]}
        return original(item, **kwargs)
    monkeypatch.setattr(env.lookup, "verify_notice", verify)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "failed"
    assert suspension_sync.summary(env.repo)["pending_symbols"] == [SYMBOL]
    env.clock[0] += timedelta(minutes=5)
    monkeypatch.setattr(env.lookup, "market_notices", lambda exchange, *args, **kwargs: {
        "notices": [notice(), notice("600001.SH")] if exchange == "SH" else [], "rows": 2, "pages": 1})
    second = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert second["status"] == "failed"
    assert env.repo.get_suspension_history()["symbol"].to_list() == ["600001.SH"]
    assert len(second["pending"]) == 1 and second["pending"][0]["symbol"] == SYMBOL
    monkeypatch.setattr(env.lookup, "verify_notice", original)
    env.clock[0] = max(
        datetime.fromisoformat(second["next_retry_at"]),
        datetime.fromisoformat(second["notice_retries"][suspension_sync._key(notice())]["next_retry_at"]),
    )
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "succeeded"


def test_bounded_notices_resume_without_requerying_complete_market(env, monkeypatch):
    monkeypatch.setattr(env.lookup, "market_notices", lambda exchange, *args, **kwargs: {
        "notices": [notice(), notice("600001.SH")] if exchange == "SH" else [], "rows": 2, "pages": 1})
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup, max_notices=1)
    assert first["status"] == "queued" and len(first["pending"]) == 1
    assert suspension_sync.summary(env.repo)["covered_count"] == 2
    env.clock[0] += timedelta(seconds=30)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup, max_notices=1)["status"] == "succeeded"
    assert env.repo.get_suspension_history().height == 2


def test_late_morning_publication_is_not_frozen_to_previous_2000(env, monkeypatch):
    env.clock[0] = datetime(2026, 11, 19, 9, tzinfo=CN_TZ)
    stamp = datetime(2026, 11, 19, 8, 45)
    monkeypatch.setattr(env.lookup, "verify_notice", lambda *a, **kw:
                        {"events": [event(stamp=stamp)], "diagnostics": []})
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "succeeded"
    history = env.repo.get_suspension_history()
    assert dated_status(history, DAY, observed_at=stamp - timedelta(seconds=1)).is_empty()
    assert dated_status(history, DAY, observed_at=stamp)[STATUS_COLUMN].to_list() == [True]


def test_resume_uses_existing_history_and_never_fakes_daily_prices(env, monkeypatch):
    from app.services.selection_suspension_repair import merge_events

    merge_events(env.repo, [{**event(), "effective_date": DAY - timedelta(days=2)}])
    monkeypatch.setattr(env.lookup, "verify_notice", lambda *a, **kw:
                        {"events": [event(suspended=False)], "diagnostics": []})
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "succeeded"
    assert dated_status(env.repo.get_suspension_history(), DAY)[STATUS_COLUMN].to_list() == [False]
    assert not (env.repo.store.data_dir / "kline_daily" / f"date={DAY}").exists()


def test_publication_failure_keeps_journal_and_retries_without_losing_event(env, monkeypatch):
    def unavailable(*args):
        raise RuntimeError("publisher unavailable")
    monkeypatch.setattr(suspension_sync, "_publish", unavailable)
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert first["status"] == "failed" and first["publish_pending"]
    assert not suspension_sync.summary(env.repo)["coverage_complete"]
    assert env.repo.get_suspension_history().height == 1
    env.clock[0] += timedelta(minutes=1)
    published = []
    monkeypatch.setattr(suspension_sync, "_publish", lambda *a: published.append(True) or
                        {"data_release_id": "release", "public_release_id": "public"})
    second = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert second["status"] == "succeeded" and not second["publish_pending"] and published == [True]
    assert second["public_release_id"] == "public" and env.calls.count(SYMBOL) == 1


@pytest.mark.parametrize("mode", ["disabled", "reader", "stop"])
def test_unauthorized_automatic_writes_do_no_work(env, monkeypatch, mode):
    stop = None
    if mode == "disabled":
        monkeypatch.setattr(suspension_sync.preferences, "get_data_sync_auto_enabled", lambda: False)
    elif mode == "reader":
        monkeypatch.setattr(settings, "service_role", "api")
    else:
        stop = SimpleNamespace(is_set=lambda: True)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup, stop=stop) is None
    assert env.calls == [] and env.repo.get_suspension_history().is_empty()


def test_cancelled_attempt_never_merges_late_evidence(env, monkeypatch):
    def verify(*args, **kwargs):
        pipeline_jobs.request_cancel(env.store.list_recent()[0]["id"])
        return {"events": [event()], "diagnostics": []}
    monkeypatch.setattr(env.lookup, "verify_notice", verify)
    state = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert state["status"] == "cancelled" and env.repo.get_suspension_history().is_empty()
    env.clock[0] += timedelta(minutes=10)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "cancelled"


def test_empty_directory_is_explicit_failure_not_zero_coverage_success(env, monkeypatch):
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool",
                        lambda *a: SimpleNamespace(symbols=[]))
    result = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert result["status"] == "failed" and not result["coverage_complete"]
    assert env.store.list_recent()[0]["status"] == "failed" and env.calls == []


def test_independent_discovery_runs_while_daily_pipeline_is_busy(env):
    job, _ = env.store.create(kind="daily_pipeline")
    env.store.start(job)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "succeeded"
    assert env.store.active_id() == job and env.store.get(job)["status"] == "running"
    env.store.succeed(job, {})


def test_orphaned_running_attempt_resumes_saved_pending_queue(env):
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup, max_notices=0)
    path = env.repo.store.data_dir / "suspension_sync/current.json"
    state = json.loads(path.read_text())
    path.write_text(json.dumps({**state, "status": "running", "next_retry_at": None}))
    second = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert first["status"] == "queued" and second["status"] == "succeeded"
    assert env.calls.count("SH") == env.calls.count("SZ") == 1


@pytest.mark.parametrize("mode", ["disabled", "stop"])
def test_interruption_resumes_when_automatic_updates_return(env, monkeypatch, mode):
    enabled, stopped = [True], [False]
    monkeypatch.setattr(suspension_sync.preferences, "get_data_sync_auto_enabled", lambda: enabled[0])
    stop = SimpleNamespace(is_set=lambda: stopped[0])
    original = env.lookup.verify_notice
    def interrupted(*args, **kwargs):
        if mode == "disabled":
            enabled[0] = False
        else:
            stopped[0] = True
        return original(*args, **kwargs)
    monkeypatch.setattr(env.lookup, "verify_notice", interrupted)
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup, stop=stop)
    assert first["status"] == "interrupted" and env.repo.get_suspension_history().is_empty()
    enabled[0], stopped[0] = True, False
    monkeypatch.setattr(env.lookup, "verify_notice", original)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup, stop=stop)["status"] == "succeeded"
    assert env.calls.count("SH") == env.calls.count("SZ") == 1


@pytest.mark.parametrize("field,value", [
    ("feeds", []), ("errors", []), ("verified", {}), ("cutoff", "2026-11-19T09:00:00"),
    ("cutoff", "2026-11-19T09:00:00+08:00"), ("status", "unknown"),
])
def test_corrupt_saved_state_fails_visibly_without_discarding_evidence(env, field, value):
    suspension_sync.request_sync(env.repo, lookup=env.lookup, max_notices=0)
    path = env.repo.store.data_dir / "suspension_sync/current.json"
    state = json.loads(path.read_text())
    path.write_text(json.dumps({**state, field: value}))
    before = path.read_bytes()
    result = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert result["status"] == "failed" and not result["coverage_complete"]
    assert suspension_sync.summary(env.repo)["status"] == "failed" and path.read_bytes() == before
    assert env.store.list_recent()[0]["status"] == "failed"


@pytest.mark.parametrize("invalid", ["clock", "symbol", "conflict"])
def test_invalid_evidence_stays_pending_and_never_overwrites_history(env, monkeypatch, invalid):
    item = event()
    if invalid == "clock":
        item["announced_at"] = env.clock[0].replace(tzinfo=None) + timedelta(seconds=1)
    elif invalid == "symbol":
        item["symbol"] = "600001.SH"
    else:
        from app.services.selection_suspension_repair import merge_events
        merge_events(env.repo, [event(suspended=False)])
    before = env.repo.get_suspension_history()
    monkeypatch.setattr(env.lookup, "verify_notice", lambda *a, **kw:
                        {"events": [item], "diagnostics": []})
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "failed"
    assert env.repo.get_suspension_history().equals(before)
    assert suspension_sync.summary(env.repo)["pending_symbols"] == [SYMBOL]


def test_directory_growth_rescans_complete_pool_instead_of_recent_hits(env):
    suspension_sync.request_sync(env.repo, lookup=env.lookup)
    path = env.repo.store.data_dir / "instruments/instruments.parquet"
    symbols = [*env.symbols, "300001.SZ"]
    pl.DataFrame({"symbol": symbols, "name": symbols}).write_parquet(path)
    env.clock[0] += timedelta(minutes=5)
    assert suspension_sync.request_sync(env.repo, lookup=env.lookup)["status"] == "succeeded"
    assert suspension_sync.summary(env.repo)["universe_count"] == 4
    assert env.calls.count("SH") == env.calls.count("SZ") == 2


def test_real_public_release_delivers_new_event_to_readonly_consumer(env, monkeypatch):
    from app.enriched_generation import get_enriched_generation
    from app.multitenancy.public_reader import open_public_repository
    from app.multitenancy.publication import PublicPublisher, release_path
    from app.services import data_release

    monkeypatch.setattr(suspension_sync, "_publish", REAL_PUBLISH)
    monkeypatch.setattr(data_release, "audit_provider_routes", lambda: {"status": "ok", "routes": {}})
    monkeypatch.setattr(settings, "service_role", "platform")
    for asset in ("stock", "index"):
        get_enriched_generation(env.repo.store.data_dir, asset)
    enriched = env.repo.store.data_dir / "kline_daily_enriched/date=2026-11-18/part.parquet"
    enriched.parent.mkdir(parents=True)
    pl.read_parquet(env.source).write_parquet(enriched)
    publisher = PublicPublisher(env.repo.store.data_dir, env.repo.store.data_dir.parent / "public",
                                write_lock=env.repo._write_lock)
    first = publisher.publish()
    old = open_public_repository(release_path(publisher.root))
    try:
        assert old.get_suspension_history().is_empty()
        result = suspension_sync.request_sync(env.repo, publisher, lookup=env.lookup)
        assert result["status"] == "succeeded" and result["public_release_id"] != first["id"]
        reader = open_public_repository(release_path(publisher.root))
        try:
            assert reader.store.read_only and reader.get_suspension_history()["symbol"].to_list() == [SYMBOL]
            release = data_release.read_current_release(reader.store.data_dir)
            assert release["release_id"] == result["data_release_id"] and release["reason"] == "suspension_sync"
            evidence = pool_evidence(reader, DAY, pl.DataFrame({"symbol": env.symbols}),
                                     observed_at=datetime(2026, 11, 19, 8))
            assert evidence.height == 3
            assert evidence.filter(pl.col(STATUS_COLUMN))["symbol"].to_list() == [SYMBOL]
            assert old.get_suspension_history().is_empty()
        finally:
            reader.db.close()
    finally:
        old.db.close()


def test_pipeline_jobs_api_exposes_full_coverage_and_retry_state(env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import pipeline

    monkeypatch.setattr(pipeline, "job_store", env.store)
    app = FastAPI()
    app.state.repo = env.repo
    app.include_router(pipeline.router)
    with TestClient(app) as client:
        assert client.get("/api/pipeline/jobs").json()["suspension_sync"]["status"] == "not_started"
        suspension_sync.request_sync(env.repo, lookup=env.lookup, max_notices=0)
        response = client.get("/api/pipeline/jobs").json()["suspension_sync"]
        assert response["status"] == "queued" and response["pending_notices"] == 1
        assert response["universe_count"] == 3 and response["covered_count"] == 2
        assert response["next_retry_at"] and not response["coverage_complete"]


def test_risk_warning_events_join_existing_scan_and_durable_archive(env, monkeypatch):
    from app.services.announced_stock_status import confirmed_status, load_risk_events

    monkeypatch.setattr(env.lookup, "verify_notice", lambda *a, **kw: {
        "events": [], "risk_warning_events": [{
            "symbol": SYMBOL, "effective_date": DAY, "announced_on": date(2026, 11, 18),
            "announced_at": datetime(2026, 11, 18, 22, 12), "is_risk_warning": True,
            "source_url": URL, "evidence_text": "明确生效日及股票身份的公告正文",
        }], "diagnostics": [],
    })
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert first["status"] == "succeeded" and first["added"] == 1
    assert env.repo.get_suspension_history().is_empty()
    assert load_risk_events(env.repo.store.data_dir).height == 1
    assert confirmed_status(env.repo, DAY, observed_at=env.clock[0])["risk_warning"] == [SYMBOL]
    assert env.published == [0]
    assert len(list((env.repo.store.data_dir / "instrument_status/announcement_evidence").glob("*.json"))) == 1
    second = suspension_sync.request_sync(env.repo, lookup=env.lookup, refresh=True)
    assert second["status"] == "succeeded" and load_risk_events(env.repo.store.data_dir).height == 1
    assert env.calls.count("SH") == env.calls.count("SZ") == 2


def test_risk_warning_new_release_reaches_readonly_consumer(env, monkeypatch):
    from app.enriched_generation import get_enriched_generation
    from app.multitenancy.public_reader import open_public_repository
    from app.multitenancy.publication import PublicPublisher, release_path
    from app.services import data_release
    from app.services.announced_stock_status import confirmed_status

    monkeypatch.setattr(suspension_sync, "_publish", REAL_PUBLISH)
    monkeypatch.setattr(data_release, "audit_provider_routes", lambda: {"status": "ok", "routes": {}})
    monkeypatch.setattr(settings, "service_role", "platform")
    for asset in ("stock", "index"):
        get_enriched_generation(env.repo.store.data_dir, asset)
    enriched = env.repo.store.data_dir / "kline_daily_enriched/date=2026-11-18/part.parquet"
    enriched.parent.mkdir(parents=True)
    pl.read_parquet(env.source).write_parquet(enriched)
    publisher = PublicPublisher(env.repo.store.data_dir, env.repo.store.data_dir.parent / "public",
                                write_lock=env.repo._write_lock)
    publisher.publish()
    monkeypatch.setattr(env.lookup, "verify_notice", lambda *a, **kw: {
        "events": [], "risk_warning_events": [{
            "symbol": SYMBOL, "effective_date": DAY, "announced_on": date(2026, 11, 18),
            "announced_at": datetime(2026, 11, 18, 22, 12), "is_risk_warning": False,
            "source_url": URL, "evidence_text": "确认摘帽公告正文",
        }], "diagnostics": [],
    })
    result = suspension_sync.request_sync(env.repo, publisher, lookup=env.lookup)
    assert result["status"] == "succeeded" and result["public_release_id"]
    reader = open_public_repository(release_path(publisher.root))
    try:
        assert reader.store.read_only
        current = confirmed_status(reader, DAY, observed_at=env.clock[0])
        assert current["risk_removed"] == [SYMBOL] and current["risk_warning"] == []
    finally:
        reader.db.close()


def test_partial_market_pages_are_durable_and_not_reported_as_coverage(env, monkeypatch):
    saved = {"begin": str(env.clock[0].date() - timedelta(days=3)),
             "end": str(env.clock[0].date() + timedelta(days=1)),
             "page": 1, "total": 51, "first_keys": [], "seen": [], "notices": []}
    attempts = []
    def feed(exchange, *args, checkpoint, on_progress):
        if exchange == "SH":
            return {"notices": [], "pages": 1, "rows": 0}
        attempts.append(checkpoint)
        if checkpoint is None:
            on_progress(saved)
            raise ValueError("budget exhausted")
        assert checkpoint == saved
        return {"notices": [], "pages": 2, "rows": 51}
    monkeypatch.setattr(env.lookup, "market_notices", feed)
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert first["status"] == "failed" and first["scans"]["SZ"] == saved
    assert suspension_sync.summary(env.repo)["pending_symbols"] == ["000635.SZ"]
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=env.store._store_dir))
    env.clock[0] += timedelta(minutes=1)
    second = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert second["status"] == "succeeded" and second["scans"] == {}
    assert attempts == [None, saved]


def test_new_status_policy_refreshes_once_without_old_failure_backoff(env, monkeypatch):
    original = env.lookup.market_notices
    monkeypatch.setattr(env.lookup, "market_notices",
                        lambda *a, **kw: (_ for _ in ()).throw(ValueError("old scan failed")))
    first = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert first["status"] == "failed"
    path = env.repo.store.data_dir / "suspension_sync/current.json"
    previous = json.loads(path.read_text())
    previous.pop("evidence_policy")
    previous["next_retry_at"] = (env.clock[0] + timedelta(minutes=15)).isoformat()
    path.write_text(json.dumps(previous))
    monkeypatch.setattr(env.lookup, "market_notices", original)
    current = suspension_sync.request_sync(env.repo, lookup=env.lookup)
    assert current["status"] == "succeeded"
    assert current["evidence_policy"] == "stock_status_v2"
    assert env.calls == ["SH", "SZ", SYMBOL]
    assert suspension_sync.summary(env.repo)["coverage_complete"]
