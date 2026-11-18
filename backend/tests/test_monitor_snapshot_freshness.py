"""Sandbox loading time must not erase a fresh dispatched market snapshot."""
from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.multitenancy import monitor_compute
from app.multitenancy.market import encode_frame
from app.strategy.monitor_rules import normalize


@pytest.mark.parametrize("startup_seconds", [18, 24])
def test_fresh_dispatched_quotes_survive_sandbox_startup(monkeypatch, startup_seconds):
    day = date(2026, 11, 18)
    dispatch = datetime(2026, 11, 18, 14, 30, 31, tzinfo=CN_TZ).timestamp()
    monkeypatch.setattr(monitor_compute.time, "time", lambda: dispatch + startup_seconds)
    monkeypatch.setattr(monitor_compute, "cn_today", lambda: day)
    frame = pl.DataFrame({
        "symbol": ["600519.SH", "600000.SH"], "date": [day, day],
        "close": [11., 12.], "quote_ts": [dispatch * 1000, (dispatch - 60) * 1000],
    })
    payload = {
        "evaluation_started_at": dispatch,
        "snapshot": {
            "captured_at": dispatch, "status": {"is_trading_hours": True},
            "days": {"stock": str(day)}, "frames": {"stock": encode_frame(frame)},
            "indices": encode_frame(pl.DataFrame()),
        },
        "rules": [normalize({
            "id": "fresh", "name": "新鲜行情", "type": "price", "scope": "all",
            "conditions": [{"field": "close", "op": ">", "value": 10}],
        })],
    }
    result = monitor_compute.evaluate_snapshot(payload, None)
    assert [event["symbol"] for event in result["events"]] == ["600519.SH"]


def test_legacy_payload_does_not_accept_stale_quotes(monkeypatch):
    day = date(2026, 11, 18)
    dispatch = datetime(2026, 11, 18, 14, 30, 31, tzinfo=CN_TZ).timestamp()
    monkeypatch.setattr(monitor_compute.time, "time", lambda: dispatch + 24)
    monkeypatch.setattr(monitor_compute, "cn_today", lambda: day)
    frame = pl.DataFrame({"symbol": ["600519.SH"], "close": [11.], "quote_ts": [dispatch * 1000]})
    payload = {
        "snapshot": {
            "captured_at": dispatch, "status": {"is_trading_hours": True},
            "days": {"stock": str(day)}, "frames": {"stock": encode_frame(frame)},
            "indices": encode_frame(pl.DataFrame()),
        },
        "rules": [normalize({
            "id": "stale", "name": "过时行情", "type": "price", "scope": "all",
            "conditions": [{"field": "close", "op": ">", "value": 10}],
        })],
    }
    assert monitor_compute.evaluate_snapshot(payload, None)["events"] == []


@pytest.mark.parametrize("stale", [False, True])
def test_minute_context_blocker_becomes_a_retry_receipt(monkeypatch, tmp_path, stale):
    from test_late_session_optional_params import signal

    from app.strategy import overnight_runtime
    from app.strategy.engine import StrategyEngine

    day = date(2026, 11, 18)
    now = datetime(2026, 11, 18, 14, 30, 15, tzinfo=CN_TZ)
    monkeypatch.setattr(monitor_compute, "cn_today", lambda: day)
    monkeypatch.setattr(monitor_compute, "cn_now", lambda: now)
    strategy = tmp_path / "late_strategy.py"
    strategy.write_text('META = {"id": "sample", "name": "测试", "asset_types": ["stock"], "timeframes": ["1m"], '
                        '"execution_backend": "minute_filter"}\n'
                        'ENTRY_SIGNALS = ["csgi_late"]\n'
                        'def filter_minute_history(df, params):\n    return df\n')
    engine = StrategyEngine([tmp_path])
    assert engine.get("sample").execution_backend == "minute_filter"
    monkeypatch.setattr(overnight_runtime, "monitor_context",
                        lambda *args: (_ for _ in ()).throw(ValueError("分钟分区尚未发布")))
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    sig["id"] = "late"
    frame = pl.DataFrame({"symbol": ["a"], "close": [11.], "date": [day],
                         "quote_ts": [now.timestamp() * 1000]})
    payload = {"evaluation_started_at": now.timestamp(), "late_task_only": stale,
               "late_task_strategy_ids": ["sample"], "late_task_definitions": {"sample": [sig]},
               "late_task_scopes": {"sample": "sample:probe"}, "state": {},
               "snapshot": {"status": {"is_trading_hours": True}, "days": {"stock": str(day)},
                            "frames": {"stock": encode_frame(frame)}, "indices": encode_frame(pl.DataFrame())},
               "rules": [normalize({"id": "late", "name": "尾盘任务", "type": "strategy",
                                    "scope": "all", "strategy_id": "sample"})]}
    result = monitor_compute.evaluate_snapshot(payload, None, engine, tmp_path)
    assert result["events"] == result["execution_events"] == []
    daily = result["results"]["sample"]
    assert daily["entry_filter_error"] == "分钟分区尚未发布"
    assert result["state"]["engine"]["late_session_tasks"]["sample:probe:csgi_late"]["retry_phase"] == "breadth"
    assert len(monitor_compute.failure_events(result["results"], payload["rules"], {}, now)[0]) == 1
