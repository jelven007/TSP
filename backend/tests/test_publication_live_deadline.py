from datetime import datetime

import pytest

from app.multitenancy.publication import PublicPublisher


@pytest.mark.parametrize("method", ["request_minute_publish", "publish_priority"])
@pytest.mark.parametrize("minute", [25, 40, 46, 54, 55])
def test_minute_publication_covers_full_live_entry_retry_window(tmp_path, monkeypatch, method, minute):
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    calls = []
    manifest = {"id": "minute-release"}

    def publish(day):
        calls.append(day)
        return manifest

    monkeypatch.setattr(publisher, "publish_minute", publish)
    at = datetime(2026, 11, 16, 14, minute)
    result = getattr(publisher, method)(at)
    if method == "publish_priority":
        assert result == manifest
        assert calls == [at.date()]
        assert publisher.minute_priority_completed_at is not None
        assert publisher.minute_completed_at is None
        assert not publisher.minute_changed.is_set()
    else:
        assert result is None and not calls
        assert publisher.minute_completed_at is not None
        assert publisher.minute_priority_completed_at is None
        assert publisher.minute_changed.is_set()
    assert not publisher.changed.is_set()


@pytest.mark.parametrize("at", [
    datetime(2026, 11, 16, 14, 24, 59),
    datetime(2026, 11, 16, 14, 55, 1),
    datetime(2026, 11, 16, 15),
    datetime(2026, 11, 14, 14, 46),
])
def test_publication_outside_retry_window_keeps_normal_timer(tmp_path, monkeypatch, at):
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")
    monkeypatch.setattr(publisher, "publish_minute", lambda _: pytest.fail("outside urgent window"))
    publisher.request_minute_publish(at)
    assert publisher.publish_priority(at) is None
    assert not publisher.minute_changed.is_set()
    assert not publisher.changed.is_set()


def test_priority_publish_failure_wakes_independent_retry(tmp_path, monkeypatch):
    publisher = PublicPublisher(tmp_path / "source", tmp_path / "public")

    def fail(_):
        raise ValueError("minute publication superseded")

    monkeypatch.setattr(publisher, "publish_minute", fail)
    with pytest.raises(ValueError, match="superseded"):
        publisher.publish_priority(datetime(2026, 11, 16, 14, 55))
    assert publisher.minute_changed.is_set()
    assert not publisher.changed.is_set()
