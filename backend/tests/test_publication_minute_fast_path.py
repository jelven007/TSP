"""Committed minutes must remain publishable while unrelated writers are busy."""
import shutil
import tempfile
import threading
from datetime import datetime

import polars as pl
import pytest

from app.enriched_generation import get_enriched_generation
from app.multitenancy.publication import PublicPublisher, release_path


def test_committed_1430_minute_publishes_while_repository_write_lock_is_busy(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-23/part.parquet"
    minute.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [datetime(2026, 11, 23, 14, 27)],
        "close": [10.0],
    }).write_parquet(minute)
    lock = threading.Lock()
    publisher = PublicPublisher(source, tmp_path / "public", lock)
    original = publisher.publish()
    temporary = minute.with_suffix(".tmp")
    pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [datetime(2026, 11, 23, 14, 30)],
        "close": [11.0],
    }).write_parquet(temporary)
    temporary.replace(minute)
    completed = threading.Event()
    replies, errors = [], []

    def publish():
        try:
            replies.append(publisher.publish_priority(datetime(2026, 11, 23, 14, 30, 15)))
        except BaseException as exc:
            errors.append(exc)
        finally:
            completed.set()

    lock.acquire()
    worker = threading.Thread(target=publish)
    worker.start()
    try:
        visible_without_unlocking = completed.wait(timeout=1)
    finally:
        lock.release()
        worker.join(timeout=10)
    assert not worker.is_alive()
    assert not errors, errors
    assert visible_without_unlocking, "14:30 minute publication waited for an unrelated repository writer"
    current = release_path(publisher.root)
    assert current.name == replies[0]["id"] != original["id"]
    name = minute.relative_to(source)
    assert pl.read_parquet(current / name)["datetime"].max() == datetime(2026, 11, 23, 14, 30)
    assert pl.read_parquet(release_path(publisher.root, original["id"]) / name)["datetime"].max() == datetime(
        2026, 11, 23, 14, 27)


def test_slow_full_publication_cannot_hold_or_replace_newer_committed_minutes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-23/part.parquet"
    financial = source / "financials/part.parquet"
    for path in (minute, financial):
        path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [datetime(2026, 11, 23, 14, 27)],
        "close": [10.0],
    }).write_parquet(minute)
    pl.DataFrame({"symbol": ["600000.SH"], "value": [1]}).write_parquet(financial)
    publisher = PublicPublisher(source, tmp_path / "public", threading.Lock())
    original = publisher.publish()
    minute.touch()
    pl.DataFrame({"symbol": ["600000.SH"], "value": [2]}).write_parquet(financial)
    entered, release_copy, visible = threading.Event(), threading.Event(), threading.Event()
    full_errors, minute_errors = [], []
    copy = shutil.copy2

    def slow_copy(source_path, target, *args, **kwargs):
        if source_path == financial:
            entered.set()
            if not release_copy.wait(timeout=10):
                raise TimeoutError("test did not release the full publication copy")
        return copy(source_path, target, *args, **kwargs)

    def full_publish():
        try:
            publisher.publish()
        except BaseException as exc:
            # A superseded capture may be rejected; it must not replace newer minutes.
            full_errors.append(exc)

    def publish_minute():
        try:
            publisher.publish_priority(datetime(2026, 11, 23, 14, 30, 15))
        except BaseException as exc:
            minute_errors.append(exc)
        finally:
            visible.set()

    monkeypatch.setattr(shutil, "copy2", slow_copy)
    full_worker = threading.Thread(target=full_publish)
    full_worker.start()
    assert entered.wait(timeout=5), "ordinary publication did not reach the slow historical copy"
    temporary = minute.with_suffix(".tmp")
    pl.DataFrame({
        "symbol": ["600000.SH"], "datetime": [datetime(2026, 11, 23, 14, 30)],
        "close": [11.0],
    }).write_parquet(temporary)
    temporary.replace(minute)
    minute_worker = threading.Thread(target=publish_minute)
    minute_worker.start()
    try:
        published_before_history_finishes = visible.wait(timeout=1)
    finally:
        release_copy.set()
        full_worker.join(timeout=10)
        minute_worker.join(timeout=10)
    assert not full_worker.is_alive() and not minute_worker.is_alive()
    assert all(isinstance(exc, ValueError) for exc in full_errors), full_errors
    assert not minute_errors, minute_errors
    assert published_before_history_finishes, "committed minute waited for historical publication"
    current = release_path(publisher.root)
    name = minute.relative_to(source)
    assert pl.read_parquet(current / name)["close"].item() == pytest.approx(11.0)
    assert pl.read_parquet(release_path(publisher.root, original["id"]) / name)["close"].item() == pytest.approx(10.0)


def test_minute_release_inherits_frozen_history_without_full_inventory(tmp_path, monkeypatch):
    from app.multitenancy.public_reader import PublicReleaseLease
    from app.services.parquet_profile import row_symbol_stats

    source = tmp_path / "source"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-23/part.parquet"
    financial = source / "financials/part.parquet"
    for path in (minute, financial):
        path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}).write_parquet(minute)
    pl.DataFrame({"symbol": ["600000.SH"], "value": [1]}).write_parquet(financial)
    publisher = PublicPublisher(source, tmp_path / "public")
    original = publisher.publish()
    pl.DataFrame({"symbol": ["600000.SH"], "value": [2]}).write_parquet(financial)
    temporary = minute.with_suffix(".tmp")
    pl.DataFrame({"symbol": ["600000.SH"], "close": [11.0]}).write_parquet(temporary)
    temporary.replace(minute)

    def no_history_scan():
        raise AssertionError("minute publication scanned unrelated history")

    monkeypatch.setattr(publisher, "inventory", no_history_scan)
    latest = publisher.publish_priority(datetime(2026, 11, 23, 14, 30, 15))
    assert latest["base_release_id"] == original["id"]
    current = release_path(publisher.root)
    assert (current / "financials").is_symlink()
    assert pl.read_parquet(current / "financials/part.parquet")["value"].item() == 1
    assert row_symbol_stats(current / "kline_minute") == (1, 1)
    assert publisher.publish_priority(datetime(2026, 11, 23, 14, 30, 16))["id"] == latest["id"]
    lease = PublicReleaseLease.acquire(current)
    try:
        assert {item.release_dir.name for item in lease.dependencies} == {original["id"]}
        assert lease.validate()["id"] == latest["id"]
    finally:
        lease.close()

    from app.multitenancy.compute_broker import ComputeBroker
    with tempfile.TemporaryDirectory(dir="/tmp") as socket_dir:
        from pathlib import Path

        with ComputeBroker(Path(socket_dir) / "compute.sock", "fixed-image", publisher.root, 1) as broker:
            command = broker.command("minute-probe", current)
    mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
    assert mounts == [
        f"type=bind,src={current},dst=/market,readonly",
        f"type=bind,src={release_path(publisher.root, original['id'])},dst=/{original['id']},readonly",
    ]

    import os
    import time

    from scripts.operations.maintain_full_host import release_candidates
    past = time.time() - 20 * 86400
    os.utime(release_path(publisher.root, original["id"]), (past, past))
    extra = publisher.root / "releases" / ("f" * 32)
    extra.mkdir()
    (extra / ".public_release.json").write_text('{"id":"' + extra.name + '"}')
    assert release_path(publisher.root, original["id"]) not in release_candidates(
        publisher.root, protected=set(), keep=2, min_age_days=1)


@pytest.mark.parametrize("failure", ["copy_conflict", "capacity", "invalid_baseline", "missing_day"])
def test_minute_capture_failure_preserves_current(tmp_path, monkeypatch, failure):
    import errno
    import json
    import os
    from types import SimpleNamespace

    source = tmp_path / "source"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-23/part.parquet"
    minute.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}).write_parquet(minute)
    publisher = PublicPublisher(source, tmp_path / "public")
    first = publisher.publish()
    minute.touch()
    if failure == "copy_conflict":
        copy = shutil.copy2

        def concurrent_replace(path, target, *args, **kwargs):
            result = copy(path, target, *args, **kwargs)
            if path == minute:
                temporary = minute.with_suffix(".tmp")
                pl.DataFrame({"symbol": ["600000.SH"], "close": [12.0]}).write_parquet(temporary)
                temporary.replace(minute)
            return result

        monkeypatch.setattr(shutil, "copy2", concurrent_replace)
    elif failure == "capacity":
        monkeypatch.setattr(os, "statvfs", lambda _: SimpleNamespace(
            f_files=1_000_000, f_ffree=100_000, f_bavail=1024, f_frsize=4096))
    elif failure == "invalid_baseline":
        marker = release_path(publisher.root) / ".public_release.json"
        content = json.loads(marker.read_text())
        content["checksums"] = {}
        marker.write_text(json.dumps(content))
    else:
        minute.unlink()
    with pytest.raises((ValueError, OSError)) as result:
        publisher.publish_priority(datetime(2026, 11, 23, 14, 30, 15))
    if failure == "capacity":
        assert result.value.errno == errno.ENOSPC
    assert release_path(publisher.root).name == first["id"]
    assert len(list((publisher.root / "releases").iterdir())) == 1


def test_minute_day_change_flattens_dependencies_and_keeps_old_day(tmp_path):
    from app.multitenancy.public_reader import PublicReleaseError, PublicReleaseLease

    source = tmp_path / "source"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-23/part.parquet"
    financial = source / "financials/part.parquet"
    for path in (minute, financial):
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}).write_parquet(path)
    publisher = PublicPublisher(source, tmp_path / "public")
    original = publisher.publish()
    minute.touch()
    first = publisher.publish_priority(datetime(2026, 11, 23, 14, 30, 15))
    tomorrow = source / "kline_minute/date=2026-11-24/part.parquet"
    tomorrow.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"], "close": [11.0]}).write_parquet(tomorrow)
    latest = publisher.publish_priority(datetime(2026, 11, 24, 14, 30, 15))
    assert latest["base_release_id"] == first["base_release_id"] == original["id"]
    current = release_path(publisher.root)
    assert pl.read_parquet(current / minute.relative_to(source))["close"].item() == 10
    assert pl.read_parquet(current / tomorrow.relative_to(source))["close"].item() == 11
    lease = PublicReleaseLease.acquire(current)
    try:
        assert len(lease.dependencies) == 1
        link = current / "financials"
        link.unlink()
        link.symlink_to(source / "financials")
        with pytest.raises(PublicReleaseError):
            lease.release_id()
    finally:
        lease.close()


def test_full_market_completion_has_an_independent_publication_lane(tmp_path, monkeypatch):
    from app.market_time import CN_TZ

    source = tmp_path / "source"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-23/part.parquet"
    minute.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}).write_parquet(minute)
    financial = source / "financials/part.parquet"
    financial.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}).write_parquet(financial)
    publisher = PublicPublisher(source, tmp_path / "public", threading.Lock())
    publisher.publish()
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 11, 23, 14, 30, 15, tzinfo=CN_TZ))
    publisher.start()
    entered, release_copy = threading.Event(), threading.Event()
    copy = shutil.copy2

    def slow_history(path, target, *args, **kwargs):
        if path == financial:
            entered.set()
            if not release_copy.wait(timeout=10):
                raise TimeoutError("historical publication was not released")
        return copy(path, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", slow_history)
    financial.touch()
    publisher.changed.set()
    assert entered.wait(timeout=5)
    temporary = minute.with_suffix(".tmp")
    pl.DataFrame({"symbol": ["600000.SH"], "close": [11.0]}).write_parquet(temporary)
    temporary.replace(minute)
    completed = threading.Event()
    commit = publisher._swap_current

    def swapped(link):
        commit(link)
        completed.set()

    monkeypatch.setattr(publisher, "_swap_current", swapped)
    try:
        publisher.request_minute_publish(datetime(2026, 11, 23, 14, 30, 15))
        visible_while_history_busy = completed.wait(timeout=2)
    finally:
        release_copy.set()
        publisher.close()
    assert visible_while_history_busy, "full-market minutes were queued behind historical publication"
    assert pl.read_parquet(release_path(publisher.root) / minute.relative_to(source))["close"].item() == 11
