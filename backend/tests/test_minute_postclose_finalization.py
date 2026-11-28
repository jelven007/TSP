"""A complete key set must not freeze provisional closing auction values."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.services import kline_periods as periods
from app.services import pipeline_jobs, post_close_sync
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 27)
SYMBOL = "000001.SZ"


@pytest.fixture
def env(tmp_path, monkeypatch):
    now = datetime(2026, 11, 27, 15, 35, tzinfo=CN_TZ)
    stamps = [datetime.combine(DAY, datetime.min.time()) + timedelta(minutes=base + minute)
              for base in (570, 780) for minute in range(1, 121)]
    source = pl.DataFrame({
        "symbol": [SYMBOL] * 240, "datetime": stamps,
        **{column: [10.] * 240 for column in ("open", "high", "low", "close")},
        "volume": [1.] * 239 + [20.], "amount": [1000.] * 239 + [20000.],
    })
    provisional = source.with_columns(*[
        pl.when(pl.col("datetime") == stamps[-1]).then(0.).otherwise(pl.col(c)).alias(c)
        for c in ("volume", "amount")
    ])
    repo = KlineRepository(DataStore(tmp_path))
    periods.persist_period(repo, "1m", provisional)
    for period in periods.LOCAL_MINUTE_PERIODS:
        periods.persist_period(repo, period, periods.aggregate_minute_period(provisional, period))
    daily = pl.DataFrame({
        "symbol": [SYMBOL], "date": [DAY],
        **{column: [10.] for column in ("open", "high", "low", "close")},
        "volume": [259.], "amount": [259000.],
    })
    periods.persist_period(repo, "1d", daily)
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(settings, "service_role", "platform")
    monkeypatch.setattr(post_close_sync, "cn_now", lambda: now)
    monkeypatch.setattr(periods, "cn_now", lambda: now)
    monkeypatch.setattr("app.market_time.cn_now", lambda: now)
    monkeypatch.setattr("app.services.repair_daily.cn_today", lambda: DAY)
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool",
                        lambda *a, **kw: SimpleNamespace(symbols=[SYMBOL]))
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": [SYMBOL]}))
    monkeypatch.setattr(repo, "refresh_cache", lambda: None)
    monkeypatch.setattr(post_close_sync.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(post_close_sync.trading_day, "is_trading_day", lambda _: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", lambda jid, fn: store.start(jid) or fn())
    monkeypatch.setattr(post_close_sync, "finalize_daily_settlement", lambda *a: None)
    monkeypatch.setattr(post_close_sync, "publish_data_release", lambda *a, **kw: {"release_id": "release"})
    replies = [source]
    calls = []

    def fetch(symbols, start, end, **kwargs):
        calls.append((symbols, start, end))
        return replies[min(len(calls) - 1, len(replies) - 1)]

    provider = SimpleNamespace(name="test", get_klines=fetch)
    monkeypatch.setattr(periods, "_period_provider", lambda _: (provider, "test"))
    monkeypatch.setattr(periods, "require_period_provider", lambda *a: (provider, "test"))
    pipeline_jobs.release_run_slot()
    yield SimpleNamespace(repo=repo, source=source, calls=calls, replies=replies, store=store)
    pipeline_jobs.release_run_slot()
    repo.db.close()


def test_postclose_replaces_provisional_closing_values_and_rebuilds_existing_buckets(env):
    state = post_close_sync.request_sync(env.repo, None)
    assert state["status"] == "succeeded"
    path = env.repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet"
    actual = pl.read_parquet(path).select(env.source.columns)
    assert actual.equals(env.source), "240 existing keys still contain the provisional 15:00 values"
    for period in periods.LOCAL_MINUTE_PERIODS:
        output = env.repo.store.data_dir / periods.TABLES[period] / f"date={DAY}" / "part.parquet"
        assert pl.read_parquet(output).equals(periods.aggregate_minute_period(env.source, period))
    assert len(env.calls) == 2, "finalization must use two fresh, agreeing source reads"
    assert all(start.date() == end.date() == DAY for _, start, end in env.calls)
    post_close_sync.request_sync(env.repo, None)
    assert len(env.calls) == 2, "persisted completion must deduplicate the next check"


def test_incomplete_final_source_keeps_original_values_and_records_failure(env):
    env.replies[:] = [env.source.head(239)]
    path = env.repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet"
    before = path.read_bytes()
    state = post_close_sync.request_sync(env.repo, None)
    assert state["status"] == "failed", "existing keys cannot mask an incomplete final source response"
    assert path.read_bytes() == before
    assert state["minute_finalization_status"] == "failed"
    assert state["periods"]["1m"]["finalization"]["rejected"][SYMBOL]


def test_conflicting_fresh_source_reads_do_not_overwrite_completed_keys(env):
    env.replies.append(env.source.with_columns(pl.col("amount") + 1))
    path = env.repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet"
    before = path.read_bytes()
    state = post_close_sync.request_sync(env.repo, None)
    assert state["status"] == "failed", "conflicting final source reads must remain unresolved"
    assert path.read_bytes() == before
    assert len(env.calls) == 2


def test_stable_but_unfinalized_source_zero_is_not_certified(env):
    env.replies[:] = [env.source.with_columns(*[
        pl.when(pl.col("datetime").dt.hour() == 15).then(0.).otherwise(pl.col(c)).alias(c)
        for c in ("volume", "amount")
    ])]
    state = post_close_sync.request_sync(env.repo, None)
    assert state["status"] == "failed"
    assert "日线合计存在缺额" in state["periods"]["1m"]["finalization"]["rejected"][SYMBOL]


def test_single_bad_stock_keeps_its_values_and_other_stock_is_finalized(env, monkeypatch):
    other = "000007.SZ"
    valid = env.source.with_columns(pl.lit(other).alias("symbol"))
    provisional = valid.with_columns(
        pl.when(pl.col("datetime").dt.hour() == 15).then(0.).otherwise(pl.col("volume")).alias("volume"),
    )
    periods.persist_period(env.repo, "1m", provisional)
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool",
                        lambda *a, **kw: SimpleNamespace(symbols=[SYMBOL, other]))
    env.replies[:] = [pl.concat([env.source.head(239), valid])]
    state = post_close_sync.request_sync(env.repo, None)
    assert state["status"] == "failed"
    assert state["periods"]["1m"]["finalization"]["verified_symbols"] == 1
    path = env.repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet"
    current = pl.read_parquet(path)
    assert current.filter(pl.col("symbol") == other).select(valid.columns).equals(valid)
    assert current.filter((pl.col("symbol") == SYMBOL) & (pl.col("datetime").dt.hour() == 15))["volume"][0] == 0


def test_finalization_failure_retries_with_new_source_then_persists_completion(env, monkeypatch):
    clock = [datetime(2026, 11, 27, 15, 35, tzinfo=CN_TZ)]
    monkeypatch.setattr(post_close_sync, "cn_now", lambda: clock[0])
    monkeypatch.setattr(periods, "cn_now", lambda: clock[0])
    env.replies[:] = [env.source.head(239)]
    failed = post_close_sync.request_sync(env.repo, None)
    calls = len(env.calls)
    assert post_close_sync.retry_pending(env.repo, None)["job_id"] == failed["job_id"]
    assert len(env.calls) == calls
    env.replies[:] = [env.source]
    clock[0] += timedelta(seconds=61)
    finished = post_close_sync.retry_pending(env.repo, None)
    assert finished["status"] == "succeeded"
    assert finished["job_id"] != failed["job_id"]
    assert finished["minute_finalization_status"] == "succeeded"
    assert len(env.calls) == calls + 2


def test_preopen_known_halt_is_not_requested_or_counted_missing(env, monkeypatch):
    from app.instrument_suspension import SCHEMA, TIMESTAMP_SCHEMA
    other = "000007.SZ"
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool",
                        lambda *a, **kw: SimpleNamespace(symbols=[SYMBOL, other]))
    history = pl.DataFrame([{
        "symbol": other, "effective_date": DAY, "announced_on": DAY,
        "is_suspended": True, "source_url": "https://exchange.example/halt",
        "announced_at": datetime(2026, 11, 27, 9, 0),
    }], schema={**SCHEMA, **TIMESTAMP_SCHEMA})
    monkeypatch.setattr("app.instrument_suspension.load_suspension_history", lambda _: history)
    result = periods.finalize_minute_day(env.repo, DAY, None)
    assert not result.get("error")
    assert result["verified_symbols"] == 1
    assert all(symbols == [SYMBOL] for symbols, _, _ in env.calls)


def test_cancel_after_source_reads_leaves_minute_and_derived_files_unchanged(env):
    paths = [env.repo.store.data_dir / periods.TABLES[p] / f"date={DAY}" / "part.parquet"
             for p in ("1m", *periods.LOCAL_MINUTE_PERIODS)]
    before = [p.read_bytes() for p in paths]

    def progress(*_):
        if len(env.calls) == 2:
            raise pipeline_jobs.JobCancelledError("test-cancel")

    with pytest.raises(pipeline_jobs.JobCancelledError):
        periods.finalize_minute_day(env.repo, DAY, None, on_progress=progress)
    assert [p.read_bytes() for p in paths] == before


def test_newer_concurrent_minute_response_wins_and_old_source_is_not_derived(env):
    from app.services.minute_write_order import begin_minute_request
    newer = env.source.with_columns(pl.col("amount") + 10)
    initial_derived = {
        p: (env.repo.store.data_dir / periods.TABLES[p] / f"date={DAY}" / "part.parquet").read_bytes()
        for p in periods.LOCAL_MINUTE_PERIODS
    }

    def progress(*_):
        if len(env.calls) == 2:
            seq = begin_minute_request(env.repo.store.data_dir / "kline_minute")
            periods.persist_period(env.repo, "1m", newer, request_seq=seq)

    result = periods.finalize_minute_day(env.repo, DAY, None, on_progress=progress)
    assert "并发更新" in result["rejected"][SYMBOL]
    assert result["verified_symbols"] == 0
    assert result["rows"] == 0
    path = env.repo.store.data_dir / "kline_minute" / f"date={DAY}" / "part.parquet"
    assert pl.read_parquet(path).select(newer.columns).equals(newer)
    for p in periods.LOCAL_MINUTE_PERIODS:
        assert (env.repo.store.data_dir / periods.TABLES[p] / f"date={DAY}" / "part.parquet").read_bytes() == initial_derived[p]
