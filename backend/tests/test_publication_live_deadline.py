from datetime import datetime

import pytest

from app.multitenancy.publication import PublicPublisher


@pytest.mark.parametrize("method", ["request_minute_publish", "publish_priority"])
@pytest.mark.parametrize("minute", [40, 46, 54, 55])
def test_minute_publication_covers_full_live_entry_retry_window(tmp_path, monkeypatch, method, minute):
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    calls = []
    monkeypatch.setattr(publisher, "publish", lambda: calls.append("published"))
    getattr(publisher, method)(datetime(2026, 11, 16, 14, minute))
    assert publisher.changed.is_set()
    assert bool(calls) is (method == "publish_priority")


@pytest.mark.parametrize("at", [
    datetime(2026, 11, 16, 14, 24, 59),
    datetime(2026, 11, 16, 14, 55, 1),
    datetime(2026, 11, 16, 15),
    datetime(2026, 11, 14, 14, 46),
])
def test_publication_outside_retry_window_keeps_normal_timer(tmp_path, monkeypatch, at):
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    monkeypatch.setattr(publisher, "publish", lambda: pytest.fail("outside urgent window"))
    publisher.request_minute_publish(at)
    publisher.publish_priority(at)
    assert not publisher.changed.is_set()
