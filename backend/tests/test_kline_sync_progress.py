from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import extend_history, kline_periods, pipeline_jobs
from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("period", ["1m", "1d"])
def test_history_progress_advances_after_each_persisted_symbol_batch(tmp_path, monkeypatch, period):
    repo = KlineRepository(DataStore(tmp_path))
    provider = SimpleNamespace()
    monkeypatch.setattr(kline_periods, "_period_provider", lambda period: (provider, "test"))
    monkeypatch.setattr(kline_periods, "fetch_period", lambda *a, **kw: pl.DataFrame())
    progress = []
    writes = []
    def persist(*args):
        writes.append(len(writes) + 1)
        return 1
    monkeypatch.setattr(kline_periods, "persist_period", persist)
    def emit(stage, pct, msg, **kwargs):
        progress.append((stage, pct, kwargs.get("stage_pct"), len(writes)))
    kline_periods.sync_period(
        repo, period, datetime(2026, 10, 23), datetime(2026, 10, 23, 16),
        symbols=[f"{i:06d}.SH" for i in range(250)], on_progress=emit,
    )
    completed = {}
    for _stage, pct, stage_pct, written in progress:
        if written:
            completed.setdefault(written, (pct, stage_pct))
    assert completed[1][0] < completed[2][0] < completed[3][0] < 100
    assert completed[1][1] < completed[2][1] < completed[3][1] == 100
    assert [pct for _, pct, _, _ in progress] == sorted(pct for _, pct, _, _ in progress)
    repo.db.close()


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_derived_minute_progress_advances_across_empty_date_segments(tmp_path, monkeypatch, period):
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *a: pytest.fail("must stay local"))
    progress = []
    try:
        result = kline_periods.sync_period(
            repo, period, datetime(2026, 10, 1), datetime(2026, 10, 11, 16),
            symbols=["600000.SH"],
            on_progress=lambda stage, pct, msg, **kw: progress.append((pct, kw["stage_pct"])),
        )
        assert result["rows"] == 0
        assert result["universe_size"] == 1
        assert sorted(set(pct for pct, _ in progress)) == [10, 38, 66, 95]
        assert progress[-1] == (95, 100)
    finally:
        repo.db.close()


def test_daily_history_resets_progress_between_daily_adj_and_enriched(tmp_path, monkeypatch):
    from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(extend_history, "_resolve_universe", lambda caps: ["600000.SH"])
    def daily(*a, **kw):
        kw["on_chunk_done"](1, 2)
        kw["on_chunk_done"](2, 2)
        return 10
    def adj(*a, **kw):
        kw["on_chunk_done"](1, 1)
        return 1, []
    monkeypatch.setattr(extend_history.kline_sync, "sync_and_persist_daily_batch", daily)
    monkeypatch.setattr(extend_history.kline_sync, "sync_adj_factor", adj)
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline", lambda: 10)
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(), Cap.ADJ_FACTOR: CapabilityLimits()})
    progress = []
    try:
        extend_history.run_extend_history(
            repo, capset, 1, "month",
            on_progress=lambda stage, pct, msg, **kw: progress.append((pct, kw.get("stage_pct"))),
        )
        by_total = dict(progress)
        assert by_total[10] == 0
        assert by_total[45] == 100
        assert by_total[48] == 0
        assert by_total[65] == 0
        assert by_total[92] == 100
        assert [pct for pct, _ in progress] == sorted(pct for pct, _ in progress)
    finally:
        repo.db.close()


def test_switching_stage_clears_previous_stage_progress(tmp_path):
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    job_id, _ = store.create(kind="extend_history")
    store.start(job_id)
    store.progress(job_id, "extend_history", 40, "日K批次", stage_pct=90)
    store.progress(job_id, "sync_adj", 48, "开始除权")
    assert store.get(job_id)["stage_pct"] == 0
    store.succeed(job_id, {})
