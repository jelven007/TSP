"""Approved fault isolation; strict public evidence and order guards remain intact."""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from test_daily_close_validation import DAY, NEXT, SYMBOL, daily, repository
from test_minute_signal_sections import scan as scan

from app.services.daily_close_validation import validate_daily_close
from app.strategy.engine import StrategyDataContext
from app.strategy.late_session_strength import previous_closes
from app.strategy.monitor import MonitorRuleEngine


def test_bad_auction_isolated_without_relaxing_strict_publication(tmp_path):
    good = daily().with_columns(pl.lit("600001.SH").alias("symbol"))
    raw = pl.concat([good, daily(68.39)])
    closing = pl.DataFrame({
        "symbol": ["600001.SH", SYMBOL],
        "datetime": [datetime(2026, 11, 4, 15)] * 2, "close": [68.30, 68.30],
    })
    repo = repository(tmp_path, raw=raw, closing=closing)
    with pytest.raises(ValueError, match=SYMBOL.replace(".", r"\.")):
        validate_daily_close(repo, DAY, rows=raw)
    rejected = validate_daily_close(repo, DAY, rows=raw, isolate_symbols=True)
    assert set(rejected) == {SYMBOL}


def test_conflicting_previous_close_only_removes_that_symbol():
    frame = pl.concat([daily(), daily().with_columns(pl.lit("600001.SH").alias("symbol"))])
    other = daily(68.39)
    context = StrategyDataContext("stock", "1m", NEXT, current=frame, daily_history=other)
    with pytest.raises(ValueError, match="来源不一致"):
        previous_closes(context)
    assert previous_closes(context, isolate_symbols=True) == {"600001.SH": 68.30}
    assert SYMBOL in context.reference_errors


def test_empty_minutes_is_waiting_not_successful_zero(scan):
    engine, context, overrides = scan
    result = engine.run("custom_test", replace(context, history=context.history.head(0)), overrides=overrides)
    assert result.sections["select"]
    assert result.section_errors["buy"]
    assert result.section_statuses["buy"] == "waiting"


def test_sell_precheck_failure_does_not_abort_buy(scan, monkeypatch):
    engine, context, overrides = scan
    monkeypatch.setattr("app.strategy.morning_liquidation.selected_definitions",
                        lambda *_: [{"id": "morning", "params": {}}])
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.total == 2
    assert "持仓" in result.section_errors["sell"]
    assert result.section_statuses["sell"] == "failed"


def test_buy_source_failure_retains_evaluated_exit(scan):
    engine, context, overrides = scan
    engine.get("custom_test").exit_signals = ["signal_exit"]
    context = replace(context, current=context.current.with_columns(pl.lit(True).alias("signal_exit")))
    engine.get("custom_test").filter_minute_history_fn = lambda *_: (_ for _ in ()).throw(
        RuntimeError("broken buy source"))
    result = engine.run("custom_test", context, overrides=overrides)
    assert result.exit_signal_hits
    assert result.sections["select"]
    assert result.sections["buy"] == []
    assert result.section_errors["buy"] == "broken buy source"
    assert result.section_statuses["buy"] == "failed"


def test_read_failure_keeps_original_cause(scan, monkeypatch):
    from app.services.screener import ScreenerService
    engine, context, overrides = scan
    service = ScreenerService(SimpleNamespace(get_instruments_asset=lambda *_: pl.DataFrame()))
    service.repo.get_daily_close_inputs = lambda day: (
        context.daily_history, context.daily_history.select("symbol", "close"))
    monkeypatch.setattr(service, "_load_enriched_for_date", lambda *_: context.current)
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: context.daily_history)
    monkeypatch.setattr(service, "_load_minute_history",
                        lambda *_: (_ for _ in ()).throw(OSError("minute partition unreadable")))
    built = service.build_strategy_context(engine, context.as_of, ["custom_test"], timeframe="1m")
    result = engine.run("custom_test", built, overrides=overrides)
    assert "minute partition unreadable" in result.section_errors["buy"]
    assert result.section_statuses["buy"] == "failed"


def test_monitor_failure_preserves_previous_success(scan, monkeypatch):
    from app.strategy import monitor_rules
    engine, context, _overrides = scan
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(engine)
    monkeypatch.setattr("app.strategy.overnight_runtime.monitor_context", lambda *_: context)
    monitor.set_rules([monitor_rules.normalize({
        "id": "strategy", "name": "strategy", "type": "strategy",
        "strategy_id": "custom_test", "scope": "all",
    })])
    monkeypatch.setattr("app.strategy.monitor.cn_today", lambda: context.as_of)
    monitor.evaluate(context.current)
    before = monitor.latest_strategy_results()["custom_test"]
    monkeypatch.setattr(engine, "run", lambda *_args, **_kw: (_ for _ in ()).throw(
        TimeoutError("compute deadline")))
    assert monitor.evaluate(context.current) == []
    after = monitor.latest_strategy_results()["custom_test"]
    assert after["rows"] == before["rows"]
    assert after["computed_at"] == before["computed_at"]
    assert after["execution_status"]["state"] == "retrying"
    assert after["execution_status"]["reason"] == "compute deadline"


@pytest.mark.parametrize("seconds,delay,expired", [
    (0, .5, False), (1, .5, True), (60, 1.5, False), (61, 1.5, True),
])
def test_realtime_confirmation_deadline_uses_saved_delay(scan, tmp_path, monkeypatch, seconds, delay, expired):
    from datetime import timedelta

    from app.strategy import custom_signals
    engine, context, overrides = scan
    monkeypatch.setattr(engine, "_user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(custom_signals, "load_intraday_all", lambda *_: [{
        "id": "trigger", "template": "late_session_strength", "enabled": True,
        "params": {"time": "14:30:00", "evaluation_delay_minutes": delay},
    }])
    context.observed_at = datetime(2026, 11, 2, 14, 35) + timedelta(seconds=seconds)
    context.realtime = True
    result = engine.run("custom_test", context, overrides=overrides)
    assert bool(result.entry_signal_hits) is not expired
    assert (result.section_statuses["buy"] == "expired") is expired
    # Explicit history scans remain useful after the live deadline.
    history = engine.run("custom_test", replace(context, realtime=False), overrides=overrides)
    assert history.total == 2


def test_failed_same_identity_does_not_overwrite_successful_archive(tmp_path):
    from app.services.strategy_results import ResultIdentity, StrategyResults
    key = ResultIdentity("custom_test", "s02", "v1", "{}", "{}", "null", "stock", "1m")
    store = StrategyResults(tmp_path)
    saved = store.save(key, {"as_of": "2026-11-04", "rows": [{"symbol": SYMBOL}], "total": 1}, started_at=1)
    attempted = store.save(key, {"as_of": "2026-11-04", "rows": [], "total": 0,
                                "entry_filter_error": "unreadable minutes"}, started_at=2)
    assert store.read(key, "2026-11-04") == saved
    assert attempted["result"]["execution_status"]["state"] == "waiting"
    assert attempted["result"]["showing_last_success"]


def test_cached_result_transmits_failure_and_last_success(monkeypatch, tmp_path):
    from test_screener_cache_api import _request

    from app.api import screener as api
    raw = {"as_of": "2026-11-04", "rows": [{"symbol": SYMBOL}], "total": 1,
           "section_statuses": {"buy": "failed"}, "execution_status": {"state": "retrying"},
           "last_success_at": 123, "showing_last_success": True}
    monkeypatch.setattr(api, "_cached_with_realtime", lambda *_: {"results": {"custom_test": raw}})
    result = api.get_cached_result("custom_test", _request(tmp_path), ext_columns=None)["result"]
    assert result["execution_status"] == raw["execution_status"]
    assert result["last_success_at"] == 123
    assert result["section_statuses"] == {"buy": "failed"}


def test_commit_crossing_deadline_keeps_sells_but_drops_new_entries():
    from app.strategy.execution_status import entry_events_before_deadline
    events = [
        {"strategy_id": "s02", "type": "buy_signal"},
        {"strategy_id": "s02", "type": "pool_entry"},
        {"strategy_id": "s02", "type": "sell_signal"},
    ]
    results = {"s02": {"entry_deadline_at": "2026-11-02T14:35:00"}}
    assert entry_events_before_deadline(events, results, datetime(2026, 11, 2, 14, 35)) == events
    assert entry_events_before_deadline(events, results, datetime(2026, 11, 2, 14, 35, 1)) == events[2:]


def test_next_preselection_isolates_one_bad_close(scan, monkeypatch):
    from app.services.screener import ScreenerService
    engine, context, overrides = scan
    reference = context.daily_history
    raw = reference.with_columns(
        pl.when(pl.col("symbol") == "600001.SH").then(10.1).otherwise(pl.col("close")).alias("close"))
    service = ScreenerService(SimpleNamespace(
        get_instruments_asset=lambda *_: pl.DataFrame(),
        get_daily_close_inputs=lambda *_: (raw, reference.select("symbol", "close")),
    ))
    monkeypatch.setattr(service, "_load_enriched_history", lambda *_: raw)
    built = service.build_selection_context(engine, reference["date"][0], context.as_of, ["custom_test"],
                                            overrides_map={"custom_test": overrides})
    assert "600001.SH" not in built.current["symbol"]
    assert "600002.SH" in built.current["symbol"]
    assert "600001.SH" in built.reference_errors
