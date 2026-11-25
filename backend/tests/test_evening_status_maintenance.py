"""The requested clock must dispatch status verification before other data."""
from datetime import datetime
from types import SimpleNamespace

from app.market_time import CN_TZ
from app.services import preferences, related_market_data


def test_2030_boundary_and_status_check_precedes_related_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(related_market_data, "sync_related_data",
                        lambda *a, **kw: calls.append("related") or {"datasets": {}})
    scheduler = related_market_data.RelatedMarketDataScheduler()
    scheduler.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    scheduler.publisher = None
    scheduler.verify_status = lambda stamp: calls.append("status") or {
        "target_date": "2026-11-24", "coverage_complete": True,
    }
    scheduler._run_scheduled(tmp_path, datetime(2026, 11, 23, 20, 29, 59, tzinfo=CN_TZ))
    assert calls == []
    scheduler._run_scheduled(tmp_path, datetime(2026, 11, 23, 20, 30, tzinfo=CN_TZ))
    assert calls == ["status", "related"]
    saved = related_market_data._load_state(tmp_path)
    assert saved["maintenance_time"] == "20:30"
    assert saved["stock_status"]["target_date"] == "2026-11-24"
    assert saved["maintenance_status"] == "completed"


def test_incomplete_status_check_cannot_be_reported_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr(related_market_data, "sync_related_data", lambda *a, **kw: {"datasets": {}})
    scheduler = related_market_data.RelatedMarketDataScheduler()
    scheduler.verify_status = lambda stamp: {"coverage_complete": False, "errors": {"SZ": "missing page"}}
    scheduler._run_scheduled(tmp_path, datetime(2026, 11, 23, 21, tzinfo=CN_TZ))
    state = related_market_data._load_state(tmp_path)
    assert state["maintenance_status"] == "partial"
    assert state["stock_status"]["errors"] == {"SZ": "missing page"}
