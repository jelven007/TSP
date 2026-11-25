"""A newly published input must unblock computation; an empty archived sell list is valid."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ


def test_new_release_unblocks_breadth_even_while_old_repair_is_running():
    from app.services.strategy_monitor_tasks import wait_for_repairs
    action = {"kind": "buy", "phase": "breadth", "signal_id": "late"}
    planning = {"actions": [action]}
    repair = {"state": "running", "request": {"attempt": 1}, "input_release_id": "old"}
    saved = {"tasks": {"buy": {"signals": {"late": {"breadth": {"repair": repair}}}}}}
    calls = []
    market = SimpleNamespace(monitor_repair=lambda request: calls.append(request) or repair)
    wait_for_repairs(planning, saved, market, current_release="new")
    assert planning["actions"] == [action]
    assert calls == []


def test_same_release_still_waits_and_keeps_repair_input_identity():
    from app.services.strategy_monitor_tasks import wait_for_repairs
    action = {"kind": "buy", "phase": "breadth", "signal_id": "late"}
    planning = {"actions": [action]}
    saved = {"tasks": {"buy": {"signals": {"late": {"breadth": {
        "repair": {"state": "running", "request": {"attempt": 1}, "input_release_id": "old"},
    }}}}}}
    market = SimpleNamespace(monitor_repair=lambda _: {"state": "running"})
    wait_for_repairs(planning, saved, market, current_release="old")
    assert planning["actions"] == []
    assert saved["tasks"]["buy"]["signals"]["late"]["breadth"]["repair"]["input_release_id"] == "old"


def test_legacy_repair_without_release_identity_still_waits():
    from app.services.strategy_monitor_tasks import wait_for_repairs
    action = {"kind": "buy", "phase": "breadth", "signal_id": "late"}
    planning = {"actions": [action]}
    saved = {"tasks": {"buy": {"signals": {"late": {"breadth": {
        "repair": {"state": "queued", "request": {"attempt": 1}},
    }}}}}}
    market = SimpleNamespace(monitor_repair=lambda _: {"state": "running"})
    wait_for_repairs(planning, saved, market, current_release="new")
    assert not planning["actions"]


@pytest.mark.parametrize("roster", [None, {"rows": []},
                         {"rows": [], "source_date": "2026-11-20"}])
def test_missing_archive_does_not_become_successful_empty_sell(monkeypatch, roster):
    from app.services.strategy_monitor_tasks import run_tasks
    from app.strategy.engine import StrategyDataContext
    monkeypatch.setattr("app.market_time.published_trading_day", lambda _: True)
    engine = SimpleNamespace(_monitor_receipt_loader=lambda _: {},
                             _selection_loader=lambda *_: None)
    now = datetime(2026, 11, 23, 15, 20, tzinfo=CN_TZ)
    context = StrategyDataContext("stock", "1m", now.date(), observed_at=now)
    windows = {"buy": [], "sell": [
        {"start": "11:15:00", "end": "13:30:00", "signal": {"id": "morning"}},
    ]}
    result = run_tasks(engine, SimpleNamespace(meta={"id": "custom_example"}),
                       context, {}, windows, roster)
    assert result.monitor_tasks["sell"]["state"] == "expired"
    assert result.monitor_tasks["sell"]["status"] == "failed"


def test_confirmed_empty_sell_roster_is_not_expired_and_keeps_prior_failure(monkeypatch):
    from app.services.strategy_monitor_tasks import run_tasks
    from app.strategy.engine import StrategyDataContext
    monkeypatch.setattr("app.market_time.published_trading_day", lambda _: True)
    failure = {"state": "expired", "terminal": True, "status": "failed",
               "errors": ["任务有效时段已结束, 未完成计算"], "attempts": 1}
    saved = {"tasks": {"sell": {"state": "expired", "terminal": True,
                               "signals": {"morning": failure}}}}
    engine = SimpleNamespace(_monitor_receipt_loader=lambda _: saved,
                             _selection_loader=lambda *_: {"rows": []})
    now = datetime(2026, 11, 23, 15, 20, tzinfo=CN_TZ)
    context = StrategyDataContext("stock", "1m", now.date(), observed_at=now)
    windows = {"buy": [], "sell": [
        {"start": "11:15:00", "end": "13:30:00", "signal": {"id": "morning"}},
    ]}
    roster = {"rows": [], "source_date": "2026-11-20", "source_record_id": "verified-empty"}
    result = run_tasks(engine, SimpleNamespace(meta={"id": "custom_mvxxcs01"}),
                       context, {}, windows, roster)
    task = result.monitor_tasks["sell"]
    assert task["state"] == "rejected"
    assert task["status"] == "succeeded" and task["count"] == 0
    assert task["errors"] == []
    assert task["completion_reason"] == "empty_archived_sell_roster"
    assert task["prior_outcome"]["signals"]["morning"] == failure
    assert saved["tasks"]["sell"]["state"] == "expired"
    assert not result.exit_signal_hits


@pytest.mark.parametrize("problem", [None, "missing", "invalid", "duplicate", "wrong_day"])
def test_breadth_repair_reuses_valid_raw_previous_close_without_indicator_rebuild(tmp_path, monkeypatch, problem):
    from app.services import kline_periods
    from app.services.monitor_input_repair import MonitorInputRepair
    now = datetime(2026, 11, 23, 14, 45, 20, tzinfo=CN_TZ)
    raw = pl.DataFrame({"symbol": ["600001.SH"], "date": [date(2026, 11, 20)], "close": [10.]})
    if problem == "missing":
        raw = raw.clear()
    elif problem == "invalid":
        raw = raw.with_columns(pl.lit(0.).alias("close"))
    elif problem == "duplicate":
        raw = pl.concat([raw, raw])
    elif problem == "wrong_day":
        raw = raw.with_columns(pl.lit(date(2026, 11, 19)).alias("date"))
    calls = []
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_daily_close_inputs=lambda day, **kwargs: (raw, pl.DataFrame()))
    service = MonitorInputRepair(SimpleNamespace(repo=repo,
        public_publisher=SimpleNamespace(publish=lambda: calls.append("publish") or "ready")),
        now=lambda: now)
    monkeypatch.setattr(kline_periods, "require_period_provider", lambda *_: (object(), None))
    def fetch(provider, period, *_args, **kwargs):
        calls.append("fetch:" + period)
        return pl.DataFrame({"symbol": ["600001.SH"]})
    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    monkeypatch.setattr(kline_periods, "persist_period", lambda _r, period, *_a, **_k: calls.append("persist:" + period))
    monkeypatch.setattr(kline_periods, "refresh_period", lambda _r, period: calls.append("refresh:" + period))
    monkeypatch.setattr("app.indicators.pipeline.run_pipeline",
                        lambda **kwargs: (_ for _ in ()).throw(AssertionError("breadth must not rebuild indicators")))
    request = {"owner": "tenant", "strategy_id": "custom_mvxxcs01", "generation": 1,
               "day": "2026-11-23", "phase": "breadth", "attempt": 1, "symbols": ["600001.SH"],
               "daily_dates": ["2026-11-20"], "minute_end": "14:45:00",
               "retry_until": "2026-11-23T14:55:00+08:00"}
    try:
        result = service.repair(request)
        assert calls == (["fetch:1m", "persist:1m", "refresh:1m", "publish"]
                         + (["fetch:1d", "persist:1d", "refresh:1d", "publish"] if problem else []))
        assert result["release"] == "ready"
    finally:
        service.close()
