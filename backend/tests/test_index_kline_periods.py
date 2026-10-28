"""指数页七只清单、独立周期表与按记录缺口修正。"""
import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.indices import router
from app.services import index_kline
from app.services.index_const import CORE_INDEX_SYMBOLS, PAGE_INDEX_NAMES
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def repo(tmp_path):
    store = DataStore(tmp_path / "data")
    yield KlineRepository(store)
    store.db.close()


def frame(days, symbols=None):
    return pl.DataFrame([
        {"symbol": symbol, "date": day, "open": float(i + 10),
         "high": float(i + 15), "low": float(i + 8), "close": float(i + 12),
         "volume": 100.0, "amount": 1000.0}
        for symbol in (symbols or ["000300.SH"]) for i, day in enumerate(days)
    ])


def test_catalog_seven_and_sidebar_four(repo):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    response = TestClient(app).get("/api/index/catalog")
    assert response.status_code == 200
    rows = response.json()["rows"]
    assert len(rows) == 7
    assert {r["symbol"]: r["name"] for r in rows} == PAGE_INDEX_NAMES
    assert PAGE_INDEX_NAMES["399330.SZ"] == "深证100"
    assert len(CORE_INDEX_SYMBOLS) == 4


@pytest.mark.parametrize("period,labels", [
    ("1w", [date(2025, 12, 29), date(2026, 1, 5)]),
    ("1mo", [date(2025, 12, 1), date(2026, 1, 1)]),
])
def test_aggregate_order_labels_and_trade_date(period, labels):
    days = [date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 5)]
    raw = frame(days).reverse()
    rows = index_kline.aggregate(raw, period).to_dicts()
    assert [row["date"] for row in rows] == labels
    assert rows[-1]["trade_date"] == days[-1]
    first_days = 3 if period == "1w" else 2
    assert rows[0]["open"] == 10
    assert rows[0]["close"] == 11 + first_days
    assert rows[0]["high"] == 14 + first_days
    assert rows[0]["low"] == 8
    assert rows[0]["volume"] == first_days * 100
    assert rows[0]["amount"] == first_days * 1000


def test_aggregate_unknown_volume_stays_unknown():
    raw = frame([date(2026, 1, 5), date(2026, 1, 6)]).with_columns(pl.lit(None).cast(pl.Float64).alias("volume"))
    assert index_kline.aggregate(raw, "1w")["volume"].item() is None


@pytest.mark.parametrize("period", ["1w", "1mo"])
def test_period_store_idempotent_readonly_and_indicators(repo, period):
    days = [date(2020 + i // 12, i % 12 + 1, 15) for i in range(72)]
    raw = frame(days)
    repo.append_index_daily(raw)
    derived = index_kline.aggregate(raw, period)
    repo.append_index_period(derived, period)
    repo.append_index_period(derived, period)
    repo.refresh_index_views()
    rows = repo.get_index_period("000300.SH", period, date(2024, 1, 1), date(2025, 12, 31))
    assert rows.height == 24
    assert "ma60" in rows.columns
    assert rows["ma5"][-1] == pytest.approx((79 + 80 + 81 + 82 + 83) / 5)
    assert not list((repo.store.data_dir / "kline_weekly").rglob("*.parquet"))
    readonly = DataStore(repo.store.data_dir, read_only=True)
    try:
        mirror = KlineRepository(readonly)
        assert mirror.get_index_period("000300.SH", period, date(2025, 1, 1), date(2025, 12, 31)).height == 12
        with pytest.raises(PermissionError):
            mirror.append_index_period(derived, period)
    finally:
        readonly.db.close()


def test_daily_missing_requests_only_absent_keys_and_keeps_existing(repo, monkeypatch):
    days = [date(2026, 9, 9), date(2026, 9, 10)]
    symbols = list(PAGE_INDEX_NAMES)
    existing = frame(days, symbols).filter(~((pl.col("symbol") == "000300.SH") & (pl.col("date") == days[1])))
    repo.append_index_daily(existing)
    repo.append_index_enriched(existing)
    calls = []
    monkeypatch.setattr(index_kline, "cn_today", lambda: days[1])
    monkeypatch.setattr(index_kline, "cn_now", lambda: datetime(2026, 9, 10, 16))
    def fetch(batch, **kwargs):
        calls.append((batch, kwargs))
        return frame(days, symbols).with_columns(pl.lit(999.0).alias("close"))
    monkeypatch.setattr(index_kline.kline_sync, "sync_daily_batch", fetch)
    result = index_kline.repair(repo, SimpleNamespace(), days[0], days[1], mode="missing")
    assert "error" not in result
    assert result["rows"] == 1 and result["remaining"] == 0
    assert len(calls) == 1 and calls[0][0] == ["000300.SH"]
    assert calls[0][1]["start_time"].date() == days[1]
    assert calls[0][1]["asset_type"] == "index"
    rows = repo.get_index_daily("000300.SH", *days)
    assert rows["close"].to_list() == [12, 999]
    assert repo.get_index_period("000300.SH", "1w", *days)["close"].item() == 999
    assert repo.get_index_period("000300.SH", "1mo", *days)["close"].item() == 999


def test_month_missing_no_remote_and_existing_value_preserved(repo, monkeypatch):
    days = [date(2026, 9, 9), date(2026, 9, 10)]
    monkeypatch.setattr(index_kline, "_trading_days", lambda start, end, **kw: days)
    monkeypatch.setattr(index_kline, "cn_today", lambda: days[-1])
    raw = frame(days, list(PAGE_INDEX_NAMES))
    repo.append_index_daily(raw)
    existing = index_kline.aggregate(raw.filter(pl.col("symbol") == "000300.SH"), "1mo").with_columns(pl.lit(888.0).alias("close"))
    repo.append_index_period(existing, "1mo")
    monkeypatch.setattr(index_kline.kline_sync, "sync_daily_batch", lambda *a, **k: pytest.fail("周月修正不能远程拉取"))
    result = index_kline.repair(repo, None, *days, period="1mo", mode="missing")
    assert "error" not in result and result["rows"] == 6
    assert repo.get_index_period("000300.SH", "1mo", *days)["close"].item() == 888


def test_missing_week_does_not_store_incomplete_base_bucket(repo, monkeypatch):
    days = [date(2026, 9, 7), date(2026, 9, 8)]
    monkeypatch.setattr(index_kline, "_trading_days", lambda start, end, **kw: days)
    monkeypatch.setattr(index_kline, "cn_today", lambda: days[-1])
    repo.append_index_daily(frame(days[:1], list(PAGE_INDEX_NAMES)))
    result = index_kline.repair(repo, None, *days, period="1w", mode="missing")
    assert result["base_remaining"] == 7
    assert result["rows"] == 0 and result["remaining"] == 7
    assert "请先补齐日K" in result["error"]
    assert not list((repo.store.data_dir / "kline_index_weekly").rglob("*.parquet"))


def test_cancel_after_fetch_preserves_unwritten_data(repo, monkeypatch):
    from app.services.pipeline_jobs import JobCancelledError
    days = [date(2026, 9, 9), date(2026, 9, 10)]
    monkeypatch.setattr(index_kline.kline_sync, "sync_daily_batch", lambda *a, **kw: frame(days, list(PAGE_INDEX_NAMES)))
    def cancel(stage, pct, message, **kw):
        if message.startswith("保存"):
            raise JobCancelledError("cancelled")
    with pytest.raises(JobCancelledError):
        index_kline.repair(repo, None, *days, on_progress=cancel)
    assert not list((repo.store.data_dir / "kline_index_daily").rglob("*.parquet"))


def test_empty_provider_is_failed_instead_of_successful_zero(repo, monkeypatch):
    monkeypatch.setattr(index_kline.kline_sync, "sync_daily_batch", lambda *a, **kw: pl.DataFrame())
    result = index_kline.repair(repo, None, date(2026, 9, 9), date(2026, 9, 10))
    assert result["rows"] == 0 and result["remaining"] == 14 and result["error"]


def test_period_api_empty_and_invalid_parameters(repo):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    client = TestClient(app)
    body = client.get("/api/index/kline", params={"symbol": "000016.SH", "period": "1mo"}).json()
    assert body["period"] == "1mo" and body["rows"] == [] and body["source"] == "none"
    assert client.get("/api/index/kline?symbol=000016.SH&period=1m").status_code == 422
    assert client.get("/api/index/kline?symbol=000016.SH&start_date=bad").status_code == 422


@pytest.mark.parametrize("body", [
    {"start_date": "20260909"}, {"start_date": "2026-09-09", "end_date": "2026-09-08"},
    {"start_date": "2026-09-09", "period": "1m"}, {"start_date": "2026-09-09", "mode": "unknown"},
])
def test_repair_rejects_invalid_request(repo, body):
    app = FastAPI()
    app.include_router(router)
    app.state.repo = repo
    assert TestClient(app).post("/api/index/repair", json=body).status_code in (400, 422)


def test_index_write_permission_and_platform_forwarding():
    from fastapi.responses import JSONResponse

    from app.multitenancy.access import middleware
    from app.multitenancy.platform import management_path, market_read_path

    account = {"id": "index-reader", "default_tenant_id": None, "auth_version": 1,
               "permissions": ["business:read", "business:write"]}
    seen = []
    class Forwarder:
        async def forward(self, request):
            seen.append(await request.json())
            return JSONResponse({"status": "started", "job_id": "platform-index"})
    app = FastAPI()
    app.middleware("http")(middleware)
    app.state.account_identity = SimpleNamespace(current_user=lambda value: account)
    app.state.platform_admin = Forwarder()
    client = TestClient(app)
    body = {"start_date": "2026-09-09", "period": "1mo", "mode": "missing"}
    response = client.post("/api/index/repair", json=body, headers={"Origin": "http://testserver"})
    assert response.status_code == 403 and not seen
    account["permissions"].append("data:manage")
    response = client.post("/api/index/repair", json=body, headers={"Origin": "http://testserver"})
    assert response.status_code == 200 and seen == [body]
    assert management_path("POST", "/api/index/repair")
    assert not management_path("GET", "/api/index/repair")
    assert market_read_path("GET", "/api/index/catalog")
    assert market_read_path("GET", "/api/index/kline")


def test_period_query_clips_last_bucket_at_requested_end(repo):
    days = [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)]
    raw = frame(days)
    repo.append_index_daily(raw)
    repo.append_index_period(index_kline.aggregate(raw, "1w"), "1w")
    row = repo.get_index_period("000300.SH", "1w", days[1], days[2]).to_dicts()[0]
    assert row["date"] == days[0] and row["trade_date"] == days[2]
    assert row["close"] == 14 and row["volume"] == 300


def test_missing_write_rechecks_snapshot_when_concurrent_writer_fills_gap(repo, monkeypatch):
    day = date(2026, 9, 9)
    repo.append_index_daily(frame([day], ["000001.SH"]))
    incoming = frame([day])
    concurrent = incoming.with_columns(pl.lit(777.0).alias("close"))
    read = pl.read_parquet
    injected = False
    def read_then_write(path, *args, **kwargs):
        nonlocal injected
        snapshot = read(path, *args, **kwargs)
        if not injected:
            injected = True
            pl.concat([snapshot, concurrent], how="diagonal_relaxed").write_parquet(path)
        return snapshot
    monkeypatch.setattr(pl, "read_parquet", read_then_write)
    assert repo.append_index_period(incoming, "1d", only_missing=True) == 0
    stored = repo.read_index_frame("1d", ["000300.SH"], day, day)
    assert stored["close"].item() == 777


def test_empty_source_catalog_still_initializes_seven_indices(repo, monkeypatch):
    from app.services import index_sync, preferences
    monkeypatch.setattr(index_sync, "_fetch_instruments_by_type", lambda *args: pl.DataFrame())
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom")
    assert index_sync.sync_index_instruments(repo, pull_etf=False) == 7
    assert set(repo.get_index_instruments()["symbol"]) == set(PAGE_INDEX_NAMES)


def test_week_month_files_are_published_and_release_manifested(repo, tmp_path):
    from app.enriched_generation import get_enriched_generation
    from app.multitenancy.publication import PublicPublisher, release_path
    from app.services.data_release import collect_dataset_snapshot
    raw = frame([date(2026, 9, 9), date(2026, 9, 10)])
    repo.append_index_daily(raw)
    repo.append_index_enriched(raw)
    for period in ("1w", "1mo"):
        repo.append_index_period(index_kline.aggregate(raw, period), period)
    for asset in ("stock", "etf", "index"):
        get_enriched_generation(repo.store.data_dir, asset)
    publisher = PublicPublisher(repo.store.data_dir, tmp_path / "public", repo._write_lock)
    publisher.publish()
    published = release_path(tmp_path / "public")
    snapshot = collect_dataset_snapshot(repo.store.data_dir)
    for period, label in (("1w", "index_weekly"), ("1mo", "index_monthly")):
        table = index_kline.TABLES[period]
        assert list((published / table).rglob("*.parquet"))
        assert snapshot[label]["partition_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "occupied"])
async def test_repair_job_lifecycle_capacity_and_terminal_state(repo, tmp_path, monkeypatch, outcome):
    from app.api import indices, kline
    from app.services import pipeline_jobs, preferences
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(kline, "_period_tasks", set())
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    days = [date(2026, 9, 9), date(2026, 9, 10)]
    monkeypatch.setattr(index_kline, "_trading_days", lambda start, end, **kwargs: days)
    if outcome == "success":
        repo.append_index_daily(frame(days, list(PAGE_INDEX_NAMES)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=None)))
    body = indices.IndexRepairRequest(start_date=str(days[0]), end_date=str(days[1]), period="1mo")
    if outcome == "occupied":
        assert pipeline_jobs.try_acquire_run_slot("existing-worker")
    try:
        result = await indices.repair_index(request, body)
        if outcome == "cancelled":
            assert pipeline_jobs.request_cancel(result["job_id"])
        repeated = await indices.repair_index(request, body)
        assert repeated == {"status": "reused", "job_id": result["job_id"]}
        async with asyncio.timeout(5):
            await asyncio.gather(*kline._period_tasks)
        job = store.get(result["job_id"])
        expected_status = {"success": "succeeded", "failure": "failed", "cancelled": "cancelled", "occupied": "failed"}[outcome]
        assert job["status"] == expected_status, job
        assert job["request"]["period"] == "1mo"
        if outcome == "success":
            assert job["result"]["rows"] == 7 and job["progress"] == 100
        elif outcome == "failure":
            assert "日K" in job["error"]
        elif outcome == "occupied":
            assert "已有数据任务" in job["error"]
            assert not pipeline_jobs.try_acquire_run_slot("should-not-release")
        if outcome != "occupied":
            assert pipeline_jobs.try_acquire_run_slot("next-worker")
    finally:
        pipeline_jobs.release_run_slot()
        preferences._invalidate_cache()
