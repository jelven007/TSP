from __future__ import annotations

from datetime import date, datetime

import polars as pl

from app.services.second_quote_archive import SecondQuoteArchive, build_snapshot_frame


def _records(price: float, source_time: str = "09:30:01.250") -> dict[str, list[dict]]:
    return {
        "stock": [
            {
                "symbol": "600519.SH",
                "last_price": price,
                "prev_close": 10.0,
                "open": 10.0,
                "high": price,
                "low": 10.0,
                "volume": 123.0,
                "amount": 129_150.0,
                "timestamp": 1_767_576_601_000,
                "source_time": source_time,
            }
        ],
        "etf": [],
        "index": [],
    }


def test_build_snapshot_frame_keeps_collection_and_source_time_separate() -> None:
    frame = build_snapshot_frame(
        _records(10.5),
        provider="rustdx",
        capture_id=123,
        collected_at_ms=1_767_576_602_000,
        trade_date=date(2026, 1, 5),
    )

    assert frame.columns == [
        "schema_version",
        "trade_date",
        "capture_id",
        "collected_at_ms",
        "provider_ts_ms",
        "source_ts_ms",
        "source_time",
        "source_date_inferred",
        "provider",
        "provider_version",
        "asset_type",
        "symbol",
        "last_price",
        "prev_close",
        "open",
        "high",
        "low",
        "volume",
        "amount",
        "change_pct",
        "change_amount",
        "amplitude",
        "turnover_rate",
        "session",
        "bid1",
        "ask1",
        "bid1_volume",
        "ask1_volume",
    ]
    row = frame.row(0, named=True)
    assert row["capture_id"] == 123
    assert row["collected_at_ms"] == 1_767_576_602_000
    assert row["provider_ts_ms"] == 1_767_576_601_000
    assert row["source_time"] == "09:30:01.250"
    assert row["source_ts_ms"] == 1_767_576_601_250
    assert row["source_date_inferred"] is True
    assert row["asset_type"] == "stock"


def test_archive_writes_immutable_hour_shards_and_drains_on_stop(tmp_path) -> None:
    archive = SecondQuoteArchive(
        tmp_path,
        enabled=lambda: True,
        retention_days=lambda: 5,
        flush_rounds=1,
        queue_capacity=4,
    )

    assert archive.submit(
        _records(10.5),
        provider="rustdx",
        capture_id=100,
        collected_at_ms=1_767_576_601_000,
        trade_date=date(2026, 1, 5),
    )
    assert archive.submit(
        _records(10.6, "09:30:02.250"),
        provider="rustdx",
        capture_id=200,
        collected_at_ms=1_767_576_602_000,
        trade_date=date(2026, 1, 5),
    )

    assert archive.stop(timeout=5.0)

    root = tmp_path / "quote_second" / "date=2026-01-05" / "hour=09"
    parts = sorted(root.glob("part-*.parquet"))
    assert len(parts) == 2
    assert not list(root.glob("*.tmp"))
    frames = [pl.read_parquet(path) for path in parts]
    combined = pl.concat(frames).sort("capture_id")
    assert combined["capture_id"].to_list() == [100, 200]
    assert combined["last_price"].to_list() == [10.5, 10.6]

    status = archive.status()
    assert status["written_rounds"] == 2
    assert status["written_rows"] == 2
    assert status["written_files"] == 2
    assert status["queued_rounds"] == 0
    assert status["last_error"] is None


def test_archive_is_noop_until_explicitly_enabled(tmp_path) -> None:
    archive = SecondQuoteArchive(
        tmp_path,
        enabled=lambda: False,
        retention_days=lambda: 5,
    )

    assert not archive.submit(
        _records(10.5),
        provider="rustdx",
        capture_id=100,
        collected_at_ms=1_767_576_601_000,
        trade_date=date(2026, 1, 5),
    )
    assert archive.status()["running"] is False
    assert not (tmp_path / "quote_second").exists()


def test_quote_service_archives_without_changing_daily_write_path(monkeypatch) -> None:
    from app.services import quote_service as quote_service_module
    from app.services.quote_service import QuoteService

    class Repo:
        def __init__(self) -> None:
            self.daily_rows = 0

        def get_index_symbol_set(self) -> set[str]:
            return set()

        def get_etf_instruments(self) -> pl.DataFrame:
            return pl.DataFrame()

        def flush_live_daily(self, frame: pl.DataFrame) -> None:
            self.daily_rows += frame.height

    class Archive:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, dict]] = []

        def submit(self, records, **kwargs) -> bool:
            self.calls.append((records, kwargs))
            return True

    service = QuoteService()
    repo = Repo()
    archive = Archive()
    service._repo = repo
    service._second_quote_archive = archive
    from app.market_time import CN_TZ

    monkeypatch.setattr(quote_service_module, "cn_today", lambda: date(2026, 1, 5))
    monkeypatch.setattr(quote_service_module, "cn_now", lambda: datetime(2026, 1, 5, 9, 30, tzinfo=CN_TZ))
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda: True)
    monkeypatch.setattr(quote_service_module, "_persist_last_fetch", lambda _ms: None)
    monkeypatch.setattr(service, "_update_volume_delta", lambda *_args: None)
    monkeypatch.setattr(service, "_flush_live_enriched", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)

    service._process_full_market_records(
        _records(10.5)["stock"],
        t0=0.0,
        now_ts=0.0,
        provider_name="rustdx",
    )

    assert repo.daily_rows == 1
    assert len(archive.calls) == 1
    archived, kwargs = archive.calls[0]
    assert archived["stock"][0]["symbol"] == "600519.SH"
    assert archived["etf"] == []
    assert archived["index"] == []
    assert kwargs["provider"] == "rustdx"


def test_failed_publish_drops_bounded_batch_and_does_not_duplicate(tmp_path, monkeypatch):
    import json

    from app.services import second_quote_archive as module

    replace = module.replace_with_retry
    calls = 0

    def fail_once(src, dst):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk unavailable")
        replace(src, dst)

    monkeypatch.setattr(module, "replace_with_retry", fail_once)
    archive = SecondQuoteArchive(tmp_path, enabled=lambda: True, retention_days=lambda: 0, flush_rounds=1)
    for capture in (1, 2):
        archive.submit(_records(10 + capture), provider="rustdx", capture_id=capture,
                       collected_at_ms=1_767_576_600_000 + capture * 1000,
                       trade_date=date(2026, 1, 5))
    assert archive.stop(5)
    parts = list((tmp_path / "quote_second").glob("date=*/hour=*/*.parquet"))
    assert len(parts) == 1
    assert pl.read_parquet(parts[0])["capture_id"].to_list() == [2]
    state = json.loads(next((tmp_path / "quote_second").glob("session-*.json")).read_text())
    assert state["dropped_rounds"] == 1
    assert state["pending_rounds"] == 0
    assert state["clean_shutdown"] is True
    assert "disk unavailable" in state["last_error"]


def test_queue_full_and_stop_race_are_bounded(tmp_path, monkeypatch):
    import threading

    archive = SecondQuoteArchive(tmp_path, enabled=lambda: True, retention_days=lambda: 0, queue_capacity=1)
    entered = threading.Event()
    release = threading.Event()
    original = archive._persist_state

    def block_start(**kwargs):
        if not kwargs["clean_shutdown"]:
            entered.set()
            assert release.wait(5)
        original(**kwargs)

    monkeypatch.setattr(archive, "_persist_state", block_start)
    args = dict(provider="rustdx", trade_date=date(2026, 1, 5), collected_at_ms=1_767_576_601_000)
    assert archive.submit(_records(10), **args)
    assert entered.wait(5)
    assert not archive.submit(_records(11), **args)
    assert archive.status()["dropped_rounds"] == 1
    assert not archive.stop(timeout=0.1)
    assert not archive.submit(_records(12), **args)
    release.set()
    assert archive.stop(timeout=5)
    archive.reopen()
    assert archive.submit(_records(13), **{**args, "collected_at_ms": 1_767_576_602_000})
    assert archive.stop(5)
    assert archive.status()["written_rounds"] == 2


def test_retention_is_opt_in_and_protects_other_datasets(tmp_path, monkeypatch):
    from app.services import second_quote_archive as module

    monkeypatch.setattr(module, "cn_today", lambda: date(2026, 1, 10))
    old = tmp_path / "quote_second" / "date=2026-01-01"
    recent = tmp_path / "quote_second" / "date=2026-01-10"
    unrelated = tmp_path / "kline_daily" / "date=2026-01-01"
    for path in (old, recent, unrelated):
        path.mkdir(parents=True)
    archive = SecondQuoteArchive(tmp_path, enabled=lambda: True, retention_days=lambda: 0)
    archive._prune_if_due()
    assert old.exists()
    archive._retention_days = lambda: 5
    archive._prune_if_due()
    assert not old.exists()
    assert recent.exists() and unrelated.exists()


def test_nonfinite_quote_values_remain_unknown():
    records = _records(float("inf"))
    frame = build_snapshot_frame(records, provider="rustdx", capture_id=1,
                                 collected_at_ms=1_767_576_601_000, trade_date=date(2026, 1, 5))
    assert frame["last_price"].item() is None


def test_low_disk_drops_capture_without_publishing_partial_shard(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services import second_quote_archive as module

    monkeypatch.setattr(module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1024))
    archive = SecondQuoteArchive(tmp_path, enabled=lambda: True, retention_days=lambda: 0)
    assert archive.submit(
        _records(10), provider="rustdx", collected_at_ms=1_767_576_601_000,
        trade_date=date(2026, 1, 5),
    )
    assert archive.stop(5)
    state = archive.status()
    assert state["dropped_rounds"] == 1
    assert state["written_rows"] == 0
    assert "insufficient disk space" in state["last_error"]


def _preopen_service(monkeypatch, now: datetime):
    """QuoteService 装配一个记录 submit 的归档桩, 时间戳由 now 决定。"""
    from app.services import quote_service as quote_service_module
    from app.services.quote_service import QuoteService

    class Repo:
        def get_index_symbol_set(self) -> set[str]:
            return set()

        def get_etf_instruments(self) -> pl.DataFrame:
            return pl.DataFrame()

        def flush_live_daily(self, frame: pl.DataFrame) -> None:
            pass

    class Archive:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def submit(self, records, **kwargs) -> bool:
            self.calls.append(records)
            return True

    service = QuoteService()
    archive = Archive()
    service._repo = Repo()
    service._second_quote_archive = archive
    monkeypatch.setattr(quote_service_module, "cn_today", lambda: now.date())
    monkeypatch.setattr(quote_service_module, "cn_now", lambda: now)
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda: True)
    monkeypatch.setattr(quote_service_module, "_persist_last_fetch", lambda _ms: None)
    monkeypatch.setattr(service, "_update_volume_delta", lambda *_args: None)
    monkeypatch.setattr(service, "_flush_live_enriched", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)
    return service, archive


def test_preopen_window_archives_only_during_call_auction(monkeypatch):
    from app.market_time import CN_TZ
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_second_quote_archive_window", lambda: "preopen")

    # 09:20 在 09:15-09:25 盘前竞价窗口内 → 归档。
    service, archive = _preopen_service(
        monkeypatch, datetime(2026, 1, 5, 9, 20, tzinfo=CN_TZ)
    )
    service._process_full_market_records(
        _records(10.5)["stock"], t0=0.0, now_ts=0.0, provider_name="rustdx",
    )
    assert len(archive.calls) == 1

    # 09:30 连续竞价时段; preopen 窗口不采样 → 不归档。
    service, archive = _preopen_service(
        monkeypatch, datetime(2026, 1, 5, 9, 30, tzinfo=CN_TZ)
    )
    service._process_full_market_records(
        _records(10.5)["stock"], t0=0.0, now_ts=0.0, provider_name="rustdx",
    )
    assert archive.calls == []


def test_full_window_still_archives_during_continuous_trading(monkeypatch):
    from app.market_time import CN_TZ
    from app.services import preferences

    monkeypatch.setattr(preferences, "get_second_quote_archive_window", lambda: "full")
    service, archive = _preopen_service(
        monkeypatch, datetime(2026, 1, 5, 9, 30, tzinfo=CN_TZ)
    )
    service._process_full_market_records(
        _records(10.5)["stock"], t0=0.0, now_ts=0.0, provider_name="rustdx",
    )
    assert len(archive.calls) == 1
