"""Request ordering regressions: controlled provider responses, temporary data only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, RLock
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import kline_sync
from app.services.minute_write_order import METADATA_KEY, begin_minute_request
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

DAY = datetime(2026, 10, 26, 9, 31)
SYMBOL = "600000.SH"


def bars(value=100.0, *, symbol=SYMBOL, minute=31):
    return pl.DataFrame({
        "symbol": [symbol], "datetime": [DAY.replace(minute=minute)],
        "open": [10.0], "high": [10.0], "low": [10.0], "close": [10.0],
        "volume": [value], "amount": [value * 10],
    })


def partition(root):
    return root / "kline_minute/date=2026-10-26/part.parquet"


@pytest.fixture
def history_sync(tmp_path, monkeypatch):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), _write_lock=RLock(),
        db=SimpleNamespace(execute=lambda *a: None),
    )
    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "test")
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 5)
    monkeypatch.setattr(kline_sync, "_resolve_minute_provider", lambda _: (object(), False, None))
    monkeypatch.setattr(kline_sync, "_cleanup_null_datetime_minute", lambda _: None)
    monkeypatch.setattr(kline_sync, "_migrate_symbol_to_date_partition", lambda _: None)
    monkeypatch.setattr(kline_sync, "_latest_minute_datetime", lambda _: None)
    caps = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    return lambda: kline_sync.sync_and_persist_minute([SYMBOL], repo, caps)


@pytest.mark.parametrize("repeat", range(3))
def test_late_history_response_cannot_replace_newer_commit(
    tmp_path, monkeypatch, history_sync, repeat,
):
    started, release = Event(), Event()

    def fetch(*args, on_segment, **kwargs):
        if not started.is_set():
            started.set()
            assert release.wait(10)
            on_segment(bars(100.0))
        else:
            on_segment(bars(200.0))
        return pl.DataFrame()

    monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(history_sync)
        try:
            assert started.wait(10)
            assert history_sync() == 1
        finally:
            release.set()
        old_count = old.result(timeout=10)
    assert pl.read_parquet(partition(tmp_path))["volume"].to_list() == [200.0]
    assert old_count == 0


def test_versions_are_per_key_and_newer_can_revise_downward(tmp_path):
    directory = tmp_path / "kline_minute"
    old, new, newest = [begin_minute_request(directory) for _ in range(3)]
    write = kline_sync._write_minute_partition
    write(bars(200.0), directory, request_seq=new)
    late = pl.concat([bars(100.0), bars(120.0, minute=32), bars(80.0, symbol="000001.SZ")])
    assert write(late, directory, request_seq=old) == 2
    assert write(bars(50.0), directory, request_seq=newest) == 1
    result = pl.read_parquet(partition(tmp_path))
    assert result["volume"].to_list() == [80.0, 50.0, 120.0]
    assert result.columns == bars().columns
    assert result.schema == bars().schema
    assert write(bars(200.0), directory, request_seq=new) == 0


def test_repeats_conflicts_and_unversioned_cannot_downgrade(tmp_path):
    directory = tmp_path / "kline_minute"
    seq = begin_minute_request(directory)
    write = kline_sync._write_minute_partition
    write(bars(), directory, request_seq=seq)
    before = partition(tmp_path).read_bytes()
    assert write(bars(), directory, request_seq=seq) == 0
    assert write(bars(900.0), directory, request_seq=seq) == 0
    assert write(bars(900.0), directory) == 0
    assert partition(tmp_path).read_bytes() == before
    assert write(pl.concat([bars(900.0), bars(40.0, minute=32)]),
                 directory, only_missing=True) == 1
    assert write(bars(900.0), directory, request_seq=seq + 1, only_missing=True) == 0
    assert pl.read_parquet(partition(tmp_path))["volume"].to_list() == [100.0, 40.0]
    assert write(bars(30.0, minute=32), directory, request_seq=seq) == 1


def test_legacy_null_rows_and_sort_keep_versions_aligned(tmp_path):
    directory = tmp_path / "kline_minute"
    path = partition(tmp_path)
    path.parent.mkdir(parents=True)
    pl.concat([bars(), bars(symbol="000001.SZ").with_columns(pl.lit(None).cast(pl.Datetime).alias("datetime"))]).write_parquet(path)
    seq = begin_minute_request(directory)
    kline_sync._write_minute_partition(bars(150.0, symbol="000001.SZ"), directory, request_seq=seq)
    assert kline_sync._write_minute_partition(bars(70.0), directory) == 2
    assert kline_sync._write_minute_partition(bars(60.0, symbol="000001.SZ"), directory) == 0
    result = pl.read_parquet(path)
    assert result["volume"].to_list() == [150.0, 70.0]
    assert result["datetime"].null_count() == 0


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_failed_commit_preserves_data_and_versions(tmp_path, monkeypatch, failure):
    directory = tmp_path / "kline_minute"
    old, new = [begin_minute_request(directory) for _ in range(2)]
    kline_sync._write_minute_partition(bars(), directory, request_seq=old)
    before = partition(tmp_path).read_bytes()

    def fail(*args, **kwargs):
        raise OSError("injected commit failure")

    with monkeypatch.context() as patch:
        if failure == "write":
            patch.setattr(pl.DataFrame, "write_parquet", fail)
        else:
            patch.setattr(kline_sync, "replace_with_retry", fail)
        with pytest.raises(OSError, match="injected"):
            kline_sync._write_minute_partition(bars(200.0), directory, request_seq=new)
    assert partition(tmp_path).read_bytes() == before
    assert kline_sync._write_minute_partition(bars(200.0), directory, request_seq=new) == 1
    assert kline_sync._write_minute_partition(bars(), directory, request_seq=old) == 0


@pytest.mark.parametrize("corruption", ["bad base64", "wrong length", "truncated"])
def test_corrupt_version_metadata_rejects_write(tmp_path, corruption):
    import base64
    import zlib

    directory = tmp_path / "kline_minute"
    path = partition(tmp_path)
    path.parent.mkdir(parents=True)
    value = "!!"
    if corruption == "wrong length":
        value = base64.b64encode(zlib.compress(b"\0" * 16)).decode()
    elif corruption == "truncated":
        value = base64.b64encode(zlib.compress(b"\0" * 8)[:-1]).decode()
    bars().write_parquet(path, metadata={METADATA_KEY: value})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="metadata"):
        kline_sync._write_minute_partition(bars(200.0), directory, request_seq=1)
    assert path.read_bytes() == before


def test_processes_share_request_order_and_restart_does_not_reset(tmp_path):
    import subprocess
    import sys

    directory = tmp_path / "kline_minute"
    script = """
import sys
from pathlib import Path
from app.services.minute_write_order import begin_minute_request
print(begin_minute_request(Path(sys.argv[1])), flush=True)
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", script, str(directory)],
                         stdout=subprocess.PIPE, text=True)
        for _ in range(4)
    ]
    sequences = []
    for process in processes:
        output, _ = process.communicate(timeout=30)
        assert process.returncode == 0
        sequences.append(int(output))
    assert sorted(sequences) == [1, 2, 3, 4]
    assert begin_minute_request(directory) == 5
    kline_sync._write_minute_partition(bars(200.0), directory, request_seq=5)
    # A different process receiving an older response must read the persisted footer.
    script = """
import sys
from pathlib import Path
from datetime import datetime
import polars as pl
from app.services.kline_sync import _write_minute_partition
frame = pl.DataFrame({"symbol": ["600000.SH"], "datetime": [datetime(2026,10,26,9,31)],
                      "volume": [100.0]})
assert _write_minute_partition(frame, Path(sys.argv[1]), request_seq=1) == 0
"""
    subprocess.run([sys.executable, "-c", script, str(directory)], check=True, timeout=30)
    assert pl.read_parquet(partition(tmp_path))["volume"].to_list() == [200.0]


def test_corrupt_counter_aborts_before_fetch(tmp_path, monkeypatch, history_sync):
    directory = tmp_path / "kline_minute"
    begin_minute_request(directory)
    (directory / ".minute-request-sequence").write_text("broken")
    monkeypatch.setattr(kline_sync, "sync_minute_batch", lambda *a, **kw: pytest.fail("must not fetch"))
    with pytest.raises(ValueError, match="sequence"):
        history_sync()
    assert not partition(tmp_path).exists()


def test_counter_commit_failure_aborts_without_reusing_allocated_sequence(tmp_path, monkeypatch):
    from app.tickflow import repository

    directory = tmp_path / "kline_minute"
    assert begin_minute_request(directory) == 1
    with monkeypatch.context() as patch:
        def fail(*a, **kw):
            raise OSError("counter replace failed")
        patch.setattr(repository, "replace_with_retry", fail)
        with pytest.raises(OSError, match="counter replace"):
            begin_minute_request(directory)
    assert begin_minute_request(directory) == 2


def test_public_inventory_has_only_standard_data_files(tmp_path):
    from app.multitenancy.publication import PublicPublisher

    source = tmp_path / "source"
    directory = source / "kline_minute"
    seq = begin_minute_request(directory)
    kline_sync._write_minute_partition(bars(), directory, request_seq=seq)
    files = PublicPublisher(source, tmp_path / "public").inventory()
    minute_files = [name for name in files if name.startswith("kline_minute/")]
    assert minute_files == ["kline_minute/date=2026-10-26/part.parquet"]
    assert pl.scan_parquet(partition(source)).collect_schema() == bars().schema


@pytest.mark.parametrize("entry", ["refresh", "priority", "period", "api", "api_partial"])
def test_actual_entry_points_preserve_newer_data(tmp_path, monkeypatch, entry):
    from app.api import kline as api
    from app.market_time import CN_TZ
    from app.services import kline_gaps, kline_periods, minute_adjust
    from app.services.minute_refresh import MinuteRefreshService
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": [SYMBOL]}))
    monkeypatch.setattr(repo, "get_index_symbol_set", lambda: set())
    monkeypatch.setattr(kline_periods, "refresh_period", lambda *a: None)
    monkeypatch.setattr(kline_gaps, "_trading_days", lambda *a, **kw: [])
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (object(), "test"))
    monkeypatch.setattr(minute_adjust, "minute_basis_is_raw", lambda _: False)
    monkeypatch.setattr(api, "cn_now", lambda: DAY.replace(hour=15, tzinfo=CN_TZ))
    monkeypatch.setattr(api, "cn_today", lambda: DAY.date())
    monkeypatch.setattr(api, "_gzip_payload", lambda request, payload, **kw: payload)
    caps = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=caps)))
    started, release = Event(), Event()

    def fetch(*a, **kw):
        if not started.is_set():
            started.set()
            assert release.wait(10)
            if entry == "api_partial":
                return pl.concat([bars(100.0), bars(120.0, minute=32)])
            return bars(100.0)
        return bars(200.0)

    if entry in ("refresh", "priority"):
        custom = SimpleNamespace(get_intraday_latest=lambda: None)
        monkeypatch.setattr(MinuteRefreshService, "_select_mode", lambda _: "increment")
        monkeypatch.setattr(MinuteRefreshService, "_resolve_custom", lambda _: (custom, "test"))
        monkeypatch.setattr(MinuteRefreshService, "priority_symbols",
                            lambda _: [SYMBOL] if entry == "priority" else [])
        monkeypatch.setattr(kline_sync, "fetch_intraday_custom_latest", lambda *a, **kw: (fetch(), 1))

        def run():
            service = MinuteRefreshService(repo)
            service._run_round()
            return service._state.last_rows
    elif entry == "period":
        monkeypatch.setattr(kline_periods, "fetch_period", fetch)

        def run():
            return kline_periods.sync_period(
                repo, "1m", DAY.replace(hour=0, minute=0), DAY.replace(hour=15), symbols=[SYMBOL],
            )["rows"]
    else:
        monkeypatch.setattr(kline_sync, "sync_minute_batch", fetch)

        def run():
            return api.get_minute_batch(request, {"symbols": [SYMBOL], "date": str(DAY.date())})

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            old = pool.submit(run)
            try:
                assert started.wait(10)
                new_result = run()
            finally:
                release.set()
            old_result = old.result(timeout=10)
        expected = [200.0, 120.0] if entry == "api_partial" else [200.0]
        assert pl.read_parquet(partition(tmp_path))["volume"].to_list() == expected
        if entry.startswith("api"):
            assert new_result["data"][SYMBOL][0]["volume"] == 200.0
            assert [row["volume"] for row in old_result["data"][SYMBOL]] == expected
        else:
            assert new_result == 1
            assert old_result == 0
    finally:
        repo.db.close()
