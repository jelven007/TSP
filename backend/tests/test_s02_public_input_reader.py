"""Frozen input publication must not pay for unrelated market SQL views."""
from datetime import date, datetime

import polars as pl
import pytest

from app.tickflow.repository import DataStore


def test_bounded_reader_keeps_release_lease_without_global_views(tmp_path, monkeypatch):
    from app.multitenancy.public_reader import open_public_repository

    release = tmp_path / "public/releases" / ("1" * 32)
    release.mkdir(parents=True)
    (release / ".public_release.json").write_text('{"id":"' + release.name + '"}')
    monkeypatch.setattr(DataStore, "_register_views", lambda *_: pytest.fail("global SQL scan"))
    repo = open_public_repository(release, bounded=True)
    try:
        assert repo.store._public_release_lease.held
        assert repo.store._public_release_lease.validate()["id"] == repo.store.data_dir.name
        assert repo.get_daily_close_inputs(date(2026, 11, 24), include_closing_minute=False)[0].is_empty()
    finally:
        lease = repo.store._public_release_lease
        repo.store._public_release_finalizer()
    assert not lease.held


def test_minute_input_reads_only_exact_partition_and_preserves_request_order(tmp_path):
    from app.services.minute_write_order import VERSION_COLUMN, version_metadata
    from app.tickflow.repository import KlineRepository

    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    at = datetime(2026, 11, 25, 14, 30)
    part = tmp_path / "kline_minute/date=2026-11-25/part.parquet"
    part.parent.mkdir()
    frame = pl.DataFrame({"symbol": ["600001.SH"] * 3, "datetime": [at] * 3,
                          "close": [10., 12., 11.], VERSION_COLUMN: [1, 3, 2]})
    frame.drop(VERSION_COLUMN).write_parquet(part, metadata=version_metadata(frame))
    unrelated = tmp_path / "kline_minute/date=2026-11-24/part.parquet"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"broken unrelated history")
    try:
        value = repo.get_minute_inputs(["600001.SH"], at, at)
        assert value["close"].to_list() == [12.]
        assert value[VERSION_COLUMN].to_list() == [3]
        assert repo.get_minute_inputs(["600002.SH"], at, at).is_empty()
        earlier = datetime(2026, 11, 23, 14, 30)
        assert repo.get_minute_inputs(["600001.SH"], earlier, earlier).is_empty()
        with pytest.raises(Exception, match=r"Parquet|parquet"):
            bad = datetime(2026, 11, 24, 14, 30)
            repo.get_minute_inputs(["600001.SH"], bad, bad)
    finally:
        store.db.close()


def test_authority_missing_exact_partitions_waits_without_a_global_scan(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from test_strategy_input_manifest import request

    from app.services.strategy_input_authority import S02InputService
    from app.services.strategy_input_manifest import build_manifest
    root = tmp_path / "public"
    release = root / "releases" / ("1" * 32)
    release.mkdir(parents=True)
    (root / "current").symlink_to("releases/" + release.name)
    (release / ".public_release.json").write_text(json.dumps({"id": release.name, "generations": {}}))
    monkeypatch.setattr(DataStore, "_register_views", lambda *_: pytest.fail("global SQL scan"))
    service = S02InputService(SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path / "data")),
        public_publisher=SimpleNamespace(root=root)))
    req = request()
    try:
        inputs = service._inputs(req, "stocks")
        value = build_manifest(req, phase="stocks", published_at=req["bar_at"], **inputs)
        assert value["state"] == "waiting_input"
        assert all(step["missing_daily_dates"] for step in value["coverage"].values())
    finally:
        service.close()
