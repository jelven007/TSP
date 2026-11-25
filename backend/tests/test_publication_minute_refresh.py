"""A frozen minute copy survives later atomic refreshes, never copy conflicts."""
import shutil

import polars as pl
import pytest

from app.enriched_generation import get_enriched_generation
from app.multitenancy.publication import PublicPublisher, release_path


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for asset in ("stock", "index"):
        get_enriched_generation(source, asset)
    minute = source / "kline_minute/date=2026-11-23/part.parquet"
    later = source / "quote_second/date=2026-11-23/part.parquet"
    for path in (minute, later):
        path.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}).write_parquet(path)
    return PublicPublisher(source, tmp_path / "public"), minute, later


def replace_minute(minute):
    temporary = minute.with_suffix(".tmp")
    pl.DataFrame({"symbol": ["600000.SH"], "close": [11.0]}).write_parquet(temporary)
    temporary.replace(minute)


def test_minute_refresh_after_capture_publishes_frozen_then_next_version(tmp_path, monkeypatch):
    publisher, minute, later = fixture(tmp_path)
    copy = shutil.copy2
    copied = []
    inventory = publisher.inventory
    monkeypatch.setattr(publisher, "inventory", lambda: dict(sorted(
        inventory().items(), key=lambda item: (not item[0].startswith("kline_minute/"), item[0]),
    )))

    def refresh(source, target, *args, **kwargs):
        result = copy(source, target, *args, **kwargs)
        copied.append(source)
        if source == later:
            assert minute in copied
            replace_minute(minute)
            from app.services.parquet_profile import prepare_profiles
            prepare_profiles(publisher.source)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copy2", refresh)
        frozen = publisher.publish()
    name = minute.relative_to(publisher.source)
    assert pl.read_parquet(release_path(publisher.root) / name)["close"].to_list() == [10.0]
    next_release = publisher.publish()
    assert next_release["id"] != frozen["id"]
    assert pl.read_parquet(release_path(publisher.root) / name)["close"].to_list() == [11.0]


@pytest.mark.parametrize("when", ["before_copy", "during_copy", "generation", "daily"])
def test_conflicts_keep_previous_release(tmp_path, monkeypatch, when):
    publisher, minute, later = fixture(tmp_path)
    first = publisher.publish()
    minute.touch()
    later.touch()
    copy = shutil.copy2

    def conflict(source, target, *args, **kwargs):
        if source == minute and when == "before_copy":
            replace_minute(minute)
        result = copy(source, target, *args, **kwargs)
        if source == minute and when == "during_copy":
            replace_minute(minute)
        if source == later:
            if when == "generation":
                marker = publisher.source / ".matrix_generation_stock.json"
                marker.write_text('{"generation":"changed"}')
            elif when == "daily":
                path = publisher.source / "kline_daily/date=2026-11-20/part.parquet"
                path.parent.mkdir(parents=True)
                pl.DataFrame({"symbol": ["600000.SH"], "close": [11.0]}).write_parquet(path)
        return result

    monkeypatch.setattr(shutil, "copy2", conflict)
    with pytest.raises(ValueError):
        publisher.publish()
    assert release_path(publisher.root).name == first["id"]
