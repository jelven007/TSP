"""完整范围与冻结预选提示; 独立采集验证见test_selected_minute_process。"""
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


def test_full_market_round_keeps_all_members_after_selected_process_is_split(tmp_path, monkeypatch):
    events = []
    custom = SimpleNamespace(get_intraday_latest=lambda **kwargs: None)
    svc = _svc(tmp_path, monkeypatch, minute_provider="rustdx", custom=custom)
    svc._repo._inst = pl.DataFrame({"symbol": ["600001.SH", "600002.SH", "600003.SH"]})
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    svc.set_priority("tenant", {"day": "2026-11-02", "sequence": 1,
        "symbols": ["600001.SH", "600002.SH", "600003.SH"],
        "selected": ["600002.SH"]})
    svc._app_state.public_publisher = SimpleNamespace(
        request_minute_publish=lambda now: events.append("publish"))

    def fetch(provider, name, *, symbols, count=3):
        events.append(tuple(symbols))
        return pl.DataFrame({"symbol": symbols, "datetime": [datetime(2026, 11, 2, 14, 30)] * len(symbols),
                             "close": [10.] * len(symbols)}), len(symbols)

    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_custom_latest", fetch)
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition",
                        lambda df, root, *, request_seq, isolate_suspension_conflicts: (events.append("commit"), df.height)[1])
    svc._run_round()
    assert events == [("600001.SH", "600002.SH", "600003.SH"), "commit", "publish"]
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


def test_selected_members_are_separate_from_full_scope_and_expire_together(tmp_path, monkeypatch):
    from app.services import minute_refresh
    svc = _svc(tmp_path, monkeypatch)
    clock = [1.]
    monkeypatch.setattr(minute_refresh.time, "monotonic", lambda: clock[0])
    day = datetime(2026, 11, 2, 14, 30)
    svc.set_priority("tenant", {"day": str(day.date()), "sequence": 1,
                              "symbols": ["600001.SH", "600002.SH"],
                              "selected": ["600002.SH"]})
    assert svc.selected_symbols(day) == ["600002.SH"]
    clock[0] = 122.
    assert svc.selected_symbols(day) == []


def test_selected_publication_failure_retries_without_blocking_full_collection(tmp_path, monkeypatch):
    events = []
    svc = _svc(tmp_path, monkeypatch, minute_provider="rustdx",
               custom=SimpleNamespace(get_intraday_latest=lambda **kwargs: None))
    svc._repo._inst = pl.DataFrame({"symbol": ["600001.SH", "600002.SH"]})
    monkeypatch.setattr(svc, "_select_mode", lambda: "increment")
    monkeypatch.setattr("app.services.minute_refresh.cn_now",
                        lambda: datetime(2026, 11, 2, 14, 30, 15))
    svc.set_priority("tenant", {"day": "2026-11-02", "sequence": 1,
                              "symbols": ["600001.SH", "600002.SH"],
                              "selected": ["600001.SH"]})
    replies = [{"error": None}]
    svc._selected_process = SimpleNamespace(
        status=lambda: {"inflight": False},
        advance=lambda job: replies.pop() if replies else None,
        close=lambda: None,
    )
    def publish(now):
        events.append("priority")
        raise ValueError("public files changed during publication; retry")
    svc._app_state.public_publisher = SimpleNamespace(
        publish_priority=publish, request_minute_publish=lambda now: events.append("retry"))
    def fetch(provider, name, *, symbols, count=3):
        events.append(symbols)
        return pl.DataFrame({"symbol": symbols,
            "datetime": [datetime(2026, 11, 2, 14, 30)] * len(symbols)}), 1
    monkeypatch.setattr("app.services.kline_sync.fetch_intraday_custom_latest", fetch)
    monkeypatch.setattr("app.services.kline_sync._write_minute_partition", lambda frame, root, *, request_seq, isolate_suspension_conflicts: frame.height)
    svc._selected_tick()
    assert svc.status()["priority"]["publication_pending"]
    assert "publication" in svc.status()["priority"]["publication_error"]
    svc._run_round()
    svc._app_state.public_publisher.publish_priority = lambda now: events.append("recovered")
    svc._selected_tick()
    assert events == ["priority", ["600001.SH", "600002.SH"], "retry", "recovered"]
    assert svc.status()["last_error"] is None
    assert svc.status()["priority"]["publication_error"] is None
