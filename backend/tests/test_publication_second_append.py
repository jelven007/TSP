"""Append-only second shards must not starve a frozen minute publication."""
import json
import shutil

import polars as pl
import pytest

from app.enriched_generation import get_enriched_generation
from app.multitenancy.publication import PublicPublisher, release_path


def _publisher(tmp_path):
    source, public = tmp_path / "source", tmp_path / "public"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-09/part.parquet"
    minute.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}).write_parquet(minute)
    second = source / "quote_second/date=2026-11-09/hour=14/part-first.parquet"
    second.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "price": [10.0]}).write_parquet(second)
    (source / "quote_second/session-live.json").write_text('{"written_rows":1}')
    return PublicPublisher(source, public), minute, second


def test_new_second_shard_during_copy_belongs_to_next_release(tmp_path, monkeypatch):
    publisher, minute, second = _publisher(tmp_path)
    first = publisher.publish()
    minute.touch()
    copy = shutil.copy2
    added = second.with_name("part-next.parquet")

    def append_during_copy(source, target, *args, **kwargs):
        result = copy(source, target, *args, **kwargs)
        if source == minute:
            pl.DataFrame({"symbol": ["600000.SH"], "price": [11.0]}).write_parquet(added)
            (publisher.source / "quote_second/session-live.json").write_text('{"written_rows":2}')
        return result

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copy2", append_during_copy)
        frozen = publisher.publish()
    assert frozen["id"] != first["id"]
    assert added.relative_to(publisher.source).as_posix() not in frozen["files"]
    assert second.relative_to(publisher.source).as_posix() in frozen["checksums"]
    assert not (release_path(publisher.root) / "quote_second/session-live.json").exists()
    next_release = publisher.publish()
    assert added.relative_to(publisher.source).as_posix() in next_release["checksums"]


def test_archive_diagnostic_changes_do_not_invalidate_release(tmp_path):
    publisher, _, _ = _publisher(tmp_path)
    first = publisher.publish()
    (publisher.source / "quote_second/session-live.json").write_text('{"written_rows":42}')
    assert publisher.publish()["id"] == first["id"]


@pytest.mark.parametrize("change", ["modify_second", "delete_second", "add_minute"])
def test_existing_shard_changes_and_other_new_inputs_still_fail(tmp_path, monkeypatch, change):
    publisher, minute, second = _publisher(tmp_path)
    first = publisher.publish()
    minute.touch()
    copy = shutil.copy2

    def conflict_during_copy(source, target, *args, **kwargs):
        result = copy(source, target, *args, **kwargs)
        if source == minute:
            if change == "modify_second":
                second.touch()
            elif change == "delete_second":
                second.unlink()
            else:
                minute.with_name("part-new.parquet").write_bytes(minute.read_bytes())
        return result

    monkeypatch.setattr(shutil, "copy2", conflict_during_copy)
    with pytest.raises((ValueError, FileNotFoundError)):
        publisher.publish()
    assert release_path(publisher.root).name == first["id"]
    assert json.loads((release_path(publisher.root) / ".public_release.json").read_text())["id"] == first["id"]
