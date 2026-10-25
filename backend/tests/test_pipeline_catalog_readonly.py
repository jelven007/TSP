from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.services import instrument_sync, repair_daily


class ReachedDailyReadError(RuntimeError):
    pass


@pytest.mark.parametrize("entry", ["pipeline", "initial", "repair"])
def test_price_pipeline_uses_persisted_catalog_without_syncing(entry, tmp_path, monkeypatch):
    day = date(2026, 10, 23)
    symbols = ["600001.SH", "000001.SZ", "300001.SZ", "688001.SH"]
    target = tmp_path / "instruments/instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame({
        "symbol": symbols, "name": ["手工沪", "手工深", "手工创", "手工科"],
        "as_of": [day] * 4, "catalog_source": ["manual"] * 4,
    }).write_parquet(target)
    before = target.read_bytes()
    monkeypatch.setattr(
        instrument_sync, "sync_instruments",
        lambda *args, **kwargs: pytest.fail("price tasks must not fetch or rewrite the catalog"),
    )
    monkeypatch.setattr(
        "app.services.instrument_catalog.publish_catalog_change",
        lambda *args, **kwargs: pytest.fail("price tasks must not publish a catalog change"),
    )
    monkeypatch.setattr("app.services.data_integrity.scan_recent_integrity", lambda *a, **kw: [])
    monkeypatch.setattr("app.services.preferences.get_pipeline_pull_a_share", lambda: True)
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: day)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *a: None)
    monkeypatch.setattr(
        "app.services.financial_sync.sync_all",
        lambda *a, **kw: dict.fromkeys(("metrics", "income", "balance_sheet", "cash_flow", "shares"), 4),
    )
    captured = []

    def fetch(selected, *args, **kwargs):
        captured.extend(selected)
        raise ReachedDailyReadError

    monkeypatch.setattr("app.services.kline_sync.sync_and_persist_daily_batch", fetch)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        latest_daily_date=lambda: day,
        refresh_cache=lambda: None,
    )
    caps = SimpleNamespace(has=lambda _: False)
    stages = []

    def progress(stage, *args, **kwargs):
        stages.append(stage)

    with pytest.raises(ReachedDailyReadError):
        if entry == "repair":
            repair_daily.run_repair_daily(repo, caps, day, on_progress=progress)
        else:
            daily_pipeline.run_now(repo, caps, on_progress=progress, initial_sync=entry == "initial")
    assert set(captured) == set(symbols)
    assert "resolve_universe" in stages
    assert "sync_instruments" not in stages
    assert target.read_bytes() == before


def test_missing_catalog_fails_without_implicitly_downloading(tmp_path, monkeypatch):
    monkeypatch.setattr(
        instrument_sync, "sync_instruments",
        lambda *args, **kwargs: pytest.fail("missing catalog must not enable an implicit sync"),
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with pytest.raises(instrument_sync.InstrumentSnapshotUnavailableError, match="没有可用的股票池快照"):
        daily_pipeline.run_now(repo, object())
    assert not (tmp_path / "instruments/instruments.parquet").exists()


def test_first_pipeline_waits_for_independent_startup_catalog(tmp_path, monkeypatch):
    import asyncio

    from app.services import heavy_job_limiter, pipeline_jobs, pipeline_runner
    from app.services.heavy_job_limiter import HeavyJobLimiter

    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_runner, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", HeavyJobLimiter(2))
    monkeypatch.setattr(daily_pipeline, "_catalog_jobs", set())
    reads = []
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)

    def initialize(*args, **kwargs):
        snapshot = instrument_sync.load_current_stock_pool(tmp_path, require_complete=True)
        reads.append(snapshot.symbols)
        return {"universe_size": len(snapshot.symbols)}

    monkeypatch.setattr("app.services.initial_sync.run_initial_sync", initialize)

    async def exercise():
        release = asyncio.Event()

        async def startup_catalog():
            await release.wait()
            target = tmp_path / "instruments/instruments.parquet"
            target.parent.mkdir()
            pl.DataFrame({
                "symbol": ["600001.SH", "000001.SZ", "300001.SZ", "688001.SH"],
                "name": ["沪", "深", "创", "科"],
            }).write_parquet(target)

        catalog = asyncio.create_task(startup_catalog())
        daily_pipeline._catalog_jobs.add(catalog)
        response = await pipeline_runner.trigger_pipeline_job(repo, object(), source="startup")
        price = pipeline_runner._background_tasks[response["job_id"]]
        try:
            await asyncio.sleep(0.02)
            assert reads == []
            assert not price.done()
        finally:
            release.set()
            await asyncio.wait_for(price, 3)
            await catalog
            daily_pipeline._catalog_jobs.clear()
        assert store.get(response["job_id"])["status"] == "succeeded"

    asyncio.run(exercise())
    assert len(reads) == 1 and len(reads[0]) == 4
