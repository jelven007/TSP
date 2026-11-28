from datetime import datetime

import polars as pl
import pytest

from app.enriched_generation import get_enriched_generation
from app.multitenancy.publication import PublicPublisher, release_path


@pytest.mark.parametrize("priority", [True, False], ids=["priority", "ordinary"])
def test_s03_publication_exposes_exact_1000_bar_before_full_refresh(tmp_path, monkeypatch, priority):
    at = datetime(2026, 11, 26, 10)
    completed_at = at.replace(second=14)
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    minute = publisher.source / "kline_minute/date=2026-11-26/part.parquet"
    minute.parent.mkdir(parents=True)
    for asset in ("stock", "index"):
        get_enriched_generation(publisher.source, asset)

    def write_bar(bar_at, price):
        path = minute.with_suffix(".tmp")
        pl.DataFrame({"symbol": ["600000.SH"], "datetime": [bar_at], "close": [price]}).write_parquet(path)
        path.replace(minute)

    write_bar(at.replace(hour=9, minute=59), 10.0)
    previous = publisher.publish()
    write_bar(at, 10.2)
    if priority:
        assert publisher.publish_priority(completed_at) is not None, "10:00 priority fast publish missing"
    else:
        publisher.request_minute_publish(completed_at)
        assert publisher.minute_changed.is_set(), "10:00 ordinary fast publish wake missing"
        # Run one iteration through the real event consumer without starting an unrelated publisher.
        monkeypatch.setattr("app.market_time.cn_now", lambda: completed_at)
        publish = publisher.publish_minute

        def publish_once(day):
            try:
                return publish(day)
            finally:
                publisher.stopped.set()

        monkeypatch.setattr(publisher, "publish_minute", publish_once)
        # Fail immediately if the consumer refuses the wakeup and tries to wait again.
        monkeypatch.setattr(publisher.minute_changed, "wait", lambda: (
            None if publisher.minute_changed.is_set() else pytest.fail("10:00 wakeup was discarded")))
        publisher._minute_loop()
    current = release_path(publisher.root)
    assert current.name != previous["id"]
    for release, expected in ((current, at), (release_path(publisher.root, previous["id"]),
                                             at.replace(hour=9, minute=59))):
        path = release / minute.relative_to(publisher.source)
        assert pl.read_parquet(path)["datetime"].item() == expected
    assert not publisher.changed.is_set()
