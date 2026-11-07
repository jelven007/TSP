"""Priority commits must become publishable before the ordinary market tail."""
from datetime import datetime
from types import SimpleNamespace

import polars as pl
from test_minute_refresh import _svc


def test_priority_covers_full_pool_and_keeps_preselection_first(tmp_path, monkeypatch):
    svc = _svc(tmp_path, monkeypatch)
    svc.set_priority("tenant", {"day": "2026-11-02", "sequence": 1,
        "symbols": ["600001.SH", "600002.SH", "600003.SH"],
        "selected": ["600003.SH"]})
    plan = svc.priority_symbols(datetime(2026, 11, 2, 14, 30))
    assert plan == ["600003.SH", "600001.SH", "600002.SH"]
    assert svc.priority_symbols(datetime(2026, 11, 3, 14, 30)) == []
    assert "symbols" not in svc.status()


def test_priority_commit_and_notification_precede_ordinary_fetch(tmp_path, monkeypatch):
    events = []
    custom = SimpleNamespace(get_intraday_latest=lambda **kwargs: None)
    svc = _svc(tmp_path, monkeypatch, minute_provider="rustdx", custom=custom)
    svc._repo._inst = pl.DataFrame({"symbol": ["600001.SH", "600002.SH", "600003.SH"]})
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    monkeypatch.setattr(svc, "priority_symbols", lambda now=None: ["600002.SH", "600001.SH"])
    svc._app_state.public_publisher = SimpleNamespace(
        request_minute_publish=lambda now: events.append("publish"))

    def fetch(provider, name, *, symbols, count=3):
        events.append(tuple(symbols))
        return pl.DataFrame({"symbol": symbols, "datetime": [datetime(2026, 11, 2, 14, 30)] * len(symbols),
                             "close": [10.] * len(symbols)}), len(symbols)

    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_custom_latest", fetch)
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition",
                        lambda df, root: (events.append("commit"), df.height)[1])
    svc._run_round()
    assert events[:4] == [("600002.SH", "600001.SH"), "commit", "publish", ("600003.SH",)]
    assert svc.status()["last_requests"] == 3


def test_expired_or_older_plans_cannot_restore_stale_members(tmp_path, monkeypatch):
    from app.services import minute_refresh
    svc = _svc(tmp_path, monkeypatch)
    clock = [1.]
    monkeypatch.setattr(minute_refresh.time, "monotonic", lambda: clock[0])
    day = datetime(2026, 11, 2, 14, 30)
    svc.set_priority("tenant", {"day": str(day.date()), "sequence": 2,
                              "symbols": ["600001.SH"], "selected": []})
    svc.set_priority("tenant", {"day": str(day.date()), "sequence": 1,
                              "symbols": ["600002.SH"], "selected": []})
    assert svc.priority_symbols(day) == ["600001.SH"]
    clock[0] = 122.
    assert svc.priority_symbols(day) == []


def test_priority_public_release_available_before_ordinary_market_tail(tmp_path, monkeypatch):
    from app.multitenancy.publication import PublicPublisher, release_path
    custom = SimpleNamespace(get_intraday_latest=lambda **kwargs: None)
    svc = _svc(tmp_path, monkeypatch, minute_provider="rustdx", custom=custom)
    svc._repo.store.data_dir = tmp_path / "source"
    svc._repo._inst = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"]})
    publisher = PublicPublisher(svc._repo.store.data_dir, tmp_path / "public")
    from app.enriched_generation import get_enriched_generation
    for asset in ("stock", "index"):
        get_enriched_generation(svc._repo.store.data_dir, asset)
    svc._app_state.public_publisher = publisher
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    monkeypatch.setattr(svc, "priority_symbols", lambda now=None: ["600001.SH"])
    monkeypatch.setattr("app.services.minute_refresh.cn_now", lambda: datetime(2026, 11, 2, 14, 30, 15))

    def fetch(provider, name, *, symbols, count=3):
        if symbols == ["600002.SH"]:
            public = release_path(publisher.root)
            frame = pl.read_parquet(public / "kline_minute/date=2026-11-02/part.parquet")
            assert frame["symbol"].to_list() == ["600001.SH"]
            import json
            manifest = json.loads((public / ".public_release.json").read_text())
            assert "minute_completed_at" not in manifest
            assert manifest["minute_priority_completed_at"] is not None
        return pl.DataFrame({"symbol": symbols, "datetime": [datetime(2026, 11, 2, 14, 30)] * len(symbols),
                             "close": [10.] * len(symbols)}), len(symbols)
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_custom_latest", fetch)
    svc._run_round()
    assert svc.status()["last_error"] is None


def test_priority_provider_failure_does_not_starve_ordinary_group(tmp_path, monkeypatch):
    from app.services.kline_sync import MinuteFetchError
    events = []
    custom = SimpleNamespace(get_intraday_latest=lambda **kwargs: None)
    svc = _svc(tmp_path, monkeypatch, minute_provider="rustdx", custom=custom)
    svc._repo._inst = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"]})
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    monkeypatch.setattr(svc, "priority_symbols", lambda now=None: ["600001.SH"])
    def fetch(provider, name, *, symbols, count=3):
        events.append(symbols)
        if symbols == ["600001.SH"]:
            raise MinuteFetchError("priority failed", requests=2)
        return pl.DataFrame({"symbol": symbols, "datetime": [datetime(2026, 11, 2, 14, 30)] * len(symbols)}), 1
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_custom_latest", fetch)
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition", lambda frame, root: frame.height)
    svc._run_round()
    assert events == [["600001.SH"], ["600002.SH"]]
    assert svc.status()["last_requests"] == 3
    assert "priority failed" in svc.status()["last_error"]


def test_priority_publication_failure_keeps_ordinary_collection(tmp_path, monkeypatch):
    events = []
    svc = _svc(tmp_path, monkeypatch, minute_provider="rustdx",
               custom=SimpleNamespace(get_intraday_latest=lambda **kwargs: None))
    svc._repo._inst = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"]})
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    monkeypatch.setattr(svc, "priority_symbols", lambda now=None: ["600001.SH"])
    def publish(now):
        raise ValueError("public files changed during publication; retry")
    svc._app_state.public_publisher = SimpleNamespace(
        publish_priority=publish, request_minute_publish=lambda now: events.append("retry"))
    def fetch(provider, name, *, symbols, count=3):
        events.append(symbols)
        return pl.DataFrame({"symbol": symbols,
            "datetime": [datetime(2026, 11, 2, 14, 30)] * len(symbols)}), 1
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_custom_latest", fetch)
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition", lambda frame, root: frame.height)
    svc._run_round()
    assert events == [["600001.SH"], ["600002.SH"], "retry"]
    assert "publication" in svc.status()["last_error"]
