"""Custom monitoring saves a lifecycle; enabling never requires market data."""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.api import monitor_rules as api
from app.market_time import CN_TZ
from app.services.daily_selections import due_window
from app.strategy import monitor_rules


def test_custom_monitor_switch_saves_all_directions_without_running(tmp_path, monkeypatch):
    strategy = SimpleNamespace(source="custom", meta={"timeframes": ["1m"]},
                               execution_backend="minute_filter")
    monkeypatch.setattr(api, "_data_dir", lambda _: tmp_path)
    from app.api import strategy as strategy_api
    monkeypatch.setattr(strategy_api, "_get_engine", lambda _: SimpleNamespace(get=lambda _: strategy))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=None)))
    enabled = api.set_strategy_monitoring("custom_test", api.StrategyMonitoringModel(enabled=True), request)
    assert enabled["enabled"] is True
    rules = monitor_rules.load_all(tmp_path)
    assert len(rules) == 1
    assert rules[0]["direction"] == "both"
    assert rules[0]["notify_events"] == ["buy_signal", "sell_signal"]
    cancelled = api.set_strategy_monitoring("custom_test", api.StrategyMonitoringModel(enabled=False), request)
    assert cancelled["enabled"] is False
    assert not any(rule["enabled"] for rule in monitor_rules.load_all(tmp_path))


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-30T19:59:59", None),
    ("2026-10-30T20:00:00", (date(2026, 10, 30), date(2026, 11, 2))),
    ("2026-10-30T21:00:00", (date(2026, 10, 30), date(2026, 11, 2))),
    ("2026-10-30T21:00:01", None),
    ("2026-10-31T20:00:00", None),
    ("2026-11-02T08:00:00", None),
])
def test_monitored_selection_window_is_bounded_and_has_no_weekend_catchup(stamp, expected):
    assert due_window(datetime.fromisoformat(stamp).replace(tzinfo=CN_TZ),
                      monitored_only=True) == expected


def test_task_summary_is_read_only_and_new_pool_strategy_is_off(tmp_path):
    from test_minute_signal_sections import CODE
    from test_strategy_sell_tasks import SIGNAL

    from app.services.strategy_monitor_tasks import monitoring_summary
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyEngine
    (tmp_path / "custom.py").write_text(CODE)
    custom_signals.save_one(tmp_path, SIGNAL)
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    engine.get("custom_test").exit_signals = ["csgi_morning"]
    now = datetime(2026, 11, 2, 10, 50, tzinfo=CN_TZ)
    rows = monitoring_summary(engine, tmp_path, now=now)["strategies"]
    assert rows[0]["enabled"] is False
    assert {t["state"] for t in rows[0]["tasks"].values()} == {"disabled"}
    assert rows[0]["tasks"]["sell"]["windows"] == [{"start": "09:30:00", "end": "11:30:00"}]
    assert monitor_rules.load_all(tmp_path) == []


def test_nightly_failure_receipts_keep_other_tasks_and_deduplicate_alerts(tmp_path, monkeypatch):
    from app.services.strategy_monitor_tasks import selection_receipts, set_monitoring, task_receipt
    set_monitoring(tmp_path, "custom_test", "Test", True)
    task_receipt(tmp_path, "custom_test", "2026-10-30", {"tasks": {"sell": {"state": "success"}}})
    job = {"as_of": "2026-11-02", "source_date": "2026-10-30",
           "state": "failed", "errors": {"custom_test": "缺日线"}}
    now = datetime(2026, 10, 30, 20, tzinfo=CN_TZ)
    monkeypatch.setattr("time.time", lambda: now.timestamp())
    selection_receipts(tmp_path, job, now)
    selection_receipts(tmp_path, job, now)
    saved = task_receipt(tmp_path, "custom_test", "2026-10-30")["tasks"]
    assert saved["select"]["target_date"] == "2026-11-02"
    assert saved["sell"]["state"] == "success"
    assert len((tmp_path / "user_data" / "alerts.jsonl").read_text().splitlines()) == 1


def test_wakeup_does_not_skip_exact_sell_fallback_or_entry_confirmation():
    from app.services.strategy_monitor_tasks import next_task_check
    windows = {"buy": [{"start": "14:30:00", "end": "14:55:00"}],
               "sell": [{"start": "09:30:00", "end": "11:30:00"}]}
    now = datetime(2026, 11, 2, 11, 29, 50, tzinfo=CN_TZ)
    assert next_task_check(windows, now, interval=30) == now.replace(minute=30, second=0)


def test_task_windows_read_tenant_signal_storage_when_engine_has_no_source_directory(monkeypatch):
    from test_strategy_sell_tasks import SIGNAL

    from app.services.strategy_monitor_tasks import task_windows
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyEngine
    documents = object()

    def load(storage):
        assert storage is documents
        return [SIGNAL]

    monkeypatch.setattr(custom_signals, "load_all", load)
    monkeypatch.setattr(custom_signals, "scoped_definitions", lambda: None)
    engine = SimpleNamespace(documents=documents, _user_data_dir=lambda: None,
                             _effective_signals=StrategyEngine._effective_signals)
    strategy = SimpleNamespace(execution_backend="minute_filter", meta={},
                               entry_signals=[], exit_signals=["csgi_morning"])
    windows = task_windows(engine, strategy, {})
    assert windows["buy"] == []
    assert windows["sell"][0]["start"] == "09:30:00"
    assert windows["sell"][0]["end"] == "11:30:00"


def test_buy_task_window_and_status_match_real_time_cutoff_without_changing_saved_signal(monkeypatch):
    from app.services.strategy_monitor_tasks import task_windows, window_state
    signal = {"id": "late", "enabled": True, "template": "late_session_strength", "params": {
        "time": "14:50:00", "end_time": "15:00:00", "evaluation_delay_minutes": .5}}
    monkeypatch.setattr("app.strategy.s01_signals.optional_triggers", lambda _: True)
    monkeypatch.setattr("app.strategy.s01_signals.trigger_definitions",
                        lambda *_a, **_k: {"entry": [signal], "exit": []})
    windows = task_windows(None, None, {})
    assert windows["buy"][0]["end"] == "14:55:00"
    assert signal["params"]["end_time"] == "15:00:00"
    now = datetime(2026, 11, 20, 14, 55, tzinfo=CN_TZ)
    assert window_state(windows, "buy", now) == "running"
    assert window_state(windows, "buy", now.replace(second=1)) == "expired"


def test_cleared_late_start_allows_morning_monitoring_and_keeps_live_cutoff(monkeypatch):
    from app.services.strategy_monitor_tasks import task_windows, window_state
    signal = {"id": "late", "enabled": True, "template": "late_session_strength", "params": {
        "time": None, "end_time": None, "evaluation_delay_minutes": .5}}
    monkeypatch.setattr("app.strategy.s01_signals.optional_triggers", lambda _: True)
    monkeypatch.setattr("app.strategy.s01_signals.trigger_definitions",
                        lambda *_a, **_k: {"entry": [signal], "exit": []})
    windows = task_windows(None, None, {})
    assert windows["buy"][0]["start"] == "09:30:00"
    assert windows["buy"][0]["end"] == "14:55:00"
    assert window_state(windows, "buy", datetime(2026, 11, 20, 10, 30, tzinfo=CN_TZ)) == "running"
