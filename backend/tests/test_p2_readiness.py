"""Status reads distinguish retained evidence, partial success and trading clocks."""
from datetime import datetime

import polars as pl
import pytest

from app.market_time import CN_TZ


@pytest.mark.parametrize("calendar,expected", [(False, "non_trading_day"), (None, "calendar_unknown")])
def test_monitoring_calendar_is_explicit_without_running(tmp_path, monkeypatch, calendar, expected):
    from test_minute_signal_sections import CODE

    from app.services import strategy_monitor_tasks as tasks
    from app.strategy.engine import StrategyEngine
    (tmp_path / "custom.py").write_text(CODE)
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    tasks.set_monitoring(tmp_path, "custom_test", "Test", True)
    monkeypatch.setattr("app.market_time.published_trading_day", lambda _: calendar)
    value = tasks.monitoring_summary(engine, tmp_path, now=datetime(2026, 11, 22, 10, tzinfo=CN_TZ))
    row = value["strategies"][0]
    assert row["is_trading_day"] is calendar
    assert row["tasks"]["select"]["state"] == expected
    assert not (tmp_path / "user_data" / "strategy_monitor_task_runs").exists()


def test_partial_integrity_is_not_certified_complete(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.services import post_close_integrity as integrity
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    integrity._save(repo, {
        "day": "2026-11-20", "status": "failed", "attempts": 86,
        "next_retry_at": "2026-11-21T00:00:41+08:00",
        "result": {"gap_check": {"remaining": 1, "suspended_skipped": 7},
                   "close_review": {"after": {"checked_count": 5216, "valid_count": 5216,
                                             "invalid_count": 0}}},
    })
    monkeypatch.setattr(integrity, "cn_now", lambda: datetime(2026, 11, 22, 12, tzinfo=CN_TZ))
    monkeypatch.setattr(integrity.trading_day, "is_trading_day", lambda _: False)
    before = (tmp_path / "post_close_integrity/current.json").read_bytes()
    result = integrity.summary(repo)
    assert result["completion"] == "partial"
    assert result["coverage"]["valid_count"] == 5216
    assert result["coverage"]["remaining"] == 1
    assert result["retry_eligible"] is False
    assert result["status"] == "failed"
    assert (tmp_path / "post_close_integrity/current.json").read_bytes() == before


def test_financial_schema_is_separate_from_original_publication_certification(tmp_path):
    from app.services.data_release import historical_input_quality
    root = tmp_path / "financials/metrics"
    root.mkdir(parents=True)
    pl.DataFrame({"symbol": ["A"], "pub_date": ["2026-11-20"]}).write_parquet(root / "part.parquet")
    result = historical_input_quality(tmp_path)["financial_publication"]
    assert result["assessment"] == "conservative_uncertified"
    assert result["tables"]["metrics"]["publication_fields"] == ["pub_date"]
    assert result["field_missing_ratio"] is None
    assert result["unknown_ratio"] == 1
    assert result["publication_time_verified"] is False
