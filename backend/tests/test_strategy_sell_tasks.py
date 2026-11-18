"""Signal-roster exits run without positions and stay within saved signal clocks."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.strategy.engine import StrategyDataContext

DAY = date(2026, 11, 2)
SIGNAL = {
    "id": "morning", "name": "早盘", "template": "morning_liquidation",
    "kind": "exit", "timeframe": "intraday", "enabled": True, "conditions": [],
    "params": {"start_time": "09:30:00", "time": "10:50:00", "fallback_time": "11:30:00",
               "rise_pct": 6., "fall_pct": 6., "decline_3m_pct": None, "volume_ratio_min": None},
}
ROSTER = {"source_date": "2026-10-30", "rows": [
    {"symbol": "600001.SH", "entry_date": "2026-10-30", "close": 10.}]}


def setup_task(tmp_path):
    from app.strategy import custom_signals
    custom_signals.save_one(tmp_path, SIGNAL)
    strategy = SimpleNamespace(meta={}, execution_backend="minute_filter",
                               exit_signals=["csgi_morning"], entry_signals=[])
    engine = SimpleNamespace(_user_data_dir=lambda: tmp_path,
                             _effective_signals=lambda o, k, d: o.get(k, d))
    return engine, strategy


@pytest.mark.parametrize("clock,state,reason", [
    ("09:29:59", "waiting", None), ("09:30:00", "success", None),
    ("10:50:00", "success", "stop_time"), ("11:30:00", "success", "fallback"),
    ("11:30:01", "expired", None),
])
def test_signal_roster_clock_exit_requires_no_account_or_fake_quantities(tmp_path, clock, state, reason):
    from app.services.strategy_monitor_tasks import evaluate_sell_task, task_windows
    engine, strategy = setup_task(tmp_path)
    ctx = StrategyDataContext("stock", "1m", DAY, current=pl.DataFrame(),
                              history=pl.DataFrame(), daily_history=pl.DataFrame({
                                  "symbol": ["600001.SH"], "date": [date(2026, 10, 30)],
                                  "close": [10.]}), observed_at=datetime.fromisoformat(f"{DAY}T{clock}"))
    result = evaluate_sell_task(engine, strategy, ctx, {}, task_windows(engine, strategy, {}), ROSTER)
    assert result["state"] == state
    assert [h["reason"] for h in result["hits"]] == ([reason] if reason else [])
    assert ctx.positions is None
    assert all(h["signal_roster"] and "available_qty" not in h and "qty" not in h for h in result["hits"])


def test_missing_roster_is_waiting_and_confirmed_empty_is_success(tmp_path):
    from app.services.strategy_monitor_tasks import evaluate_sell_task, task_windows
    engine, strategy = setup_task(tmp_path)
    ctx = StrategyDataContext("stock", "1m", DAY, observed_at=datetime(2026, 11, 2, 10, 50))
    windows = task_windows(engine, strategy, {})
    assert evaluate_sell_task(engine, strategy, ctx, {}, windows, None)["state"] == "waiting"
    assert evaluate_sell_task(engine, strategy, ctx, {}, windows, {"rows": []})["state"] == "success"


def test_first_monitored_signal_alerts_once_across_restart():
    from app.strategy.monitor import MonitorRuleEngine
    engine = MonitorRuleEngine()
    key = ("mr", "custom_test", "stock")
    hit = {"symbol": "600001.SH", "signal_roster": True, "reason": "stop_time"}
    assert engine._new_strategy_signals(key, "sell_signal", DAY, [hit], emit_initial=True) == {"600001.SH"}
    restored = MonitorRuleEngine()
    restored.restore_state(engine.export_state())
    assert restored._new_strategy_signals(key, "sell_signal", DAY,
                                         [{**hit, "reason": "fallback"}], emit_initial=True) == set()


def test_commit_after_end_rejects_new_buy_and_sell_but_keeps_independent_receipt():
    from app.services.strategy_monitor_tasks import fence_tasks
    windows = {"buy": [{"start": "14:30:00", "end": "14:55:00"}],
               "sell": [{"start": "09:30:00", "end": "11:30:00"}]}
    daily = {"sections": {"buy": [{"symbol": "NEW"}], "sell": [{"symbol": "OLD"}]},
             "rows": [{"symbol": "NEW"}], "total": 1,
             "section_statuses": {"buy": "success", "sell": "waiting"},
             "monitor_tasks": {"buy": {"state": "success"}, "sell": {"state": "waiting"}}}
    assert fence_tasks(daily, windows, datetime(2026, 11, 2, 14, 55)) == set()
    assert fence_tasks(daily, windows, datetime(2026, 11, 2, 14, 55, 1)) == {"buy"}
    assert daily["sections"]["buy"] == [] and daily["total"] == 0
    assert daily["sections"]["sell"] == [{"symbol": "OLD"}]
    assert daily["section_statuses"]["buy"] == "expired"


def test_sell_read_failure_alerts_once_without_affecting_buy():
    from app.multitenancy.monitor_compute import failure_events
    daily = {"as_of": str(DAY), "monitor_tasks": {
        "buy": {"state": "waiting"}, "sell": {"state": "failed", "errors": ["600001.SH 缺分钟"]}}}
    rules = [{"id": "mr", "strategy_id": "custom_test", "name": "Test"}]
    events, state = failure_events({"custom_test": daily}, rules, {}, datetime(2026, 11, 2, 9, 31))
    assert len(events) == 1 and "卖" in events[0]["message"] and "缺分钟" in events[0]["message"]
    assert failure_events({"custom_test": daily}, rules, state, datetime(2026, 11, 2, 9, 32))[0] == []


@pytest.mark.parametrize("buy_failure", [False, True])
@pytest.mark.parametrize("grouped", [False, True])
def test_real_monitor_archives_first_roster_sell_even_while_buy_waits(tmp_path, monkeypatch, buy_failure, grouped):
    from test_minute_signal_sections import CODE

    from app import market_time
    from app.market_time import CN_TZ
    from app.services.screener import ScreenerService
    from app.services.strategy_monitor_tasks import task_windows
    from app.strategy import monitor, monitor_rules
    from app.strategy.engine import StrategyEngine
    from app.strategy.monitor import MonitorRuleEngine
    (tmp_path / "custom.py").write_text(CODE)
    setup_task(tmp_path)
    catalog = StrategyEngine([tmp_path])
    strategy = catalog.get("custom_test")
    strategy.exit_signals = ["csgi_morning"]
    catalog._user_data_dir = lambda: tmp_path
    windows = task_windows(catalog, strategy, {})
    if buy_failure:
        windows["buy"] = [{"start": "09:30:00", "end": "14:55:00"}]
        from app.strategy import overnight_runtime
        monkeypatch.setattr(overnight_runtime, "monitor_context",
                            lambda *_: (_ for _ in ()).throw(ValueError("broken buy input")))
    catalog._monitor_task_loader = lambda sid: windows
    catalog._sell_roster_loader = lambda *_: ROSTER
    at = datetime(2026, 11, 2, 10, 50, tzinfo=CN_TZ)
    monkeypatch.setattr(market_time, "cn_now", lambda: at)
    monkeypatch.setattr(monitor, "cn_today", lambda: DAY)
    monkeypatch.setattr(ScreenerService, "_load_enriched_history", lambda *_: pl.DataFrame({
        "symbol": ["600001.SH"], "date": [date(2026, 10, 30)], "close": [10.]}))
    service = ScreenerService(SimpleNamespace(get_minute_batch=lambda *_a, **_k: pl.DataFrame()))
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(catalog)
    engine.set_data_dir(tmp_path)
    engine.set_history_loader(service._load_enriched_history)
    engine.set_rules([monitor_rules.normalize({
        "id": "custom_rule", "type": "strategy", "scope": "all", "strategy_id": "custom_test",
        "direction": "both", "notify_events": ["buy_signal", "sell_signal"], "cooldown_seconds": 0,
    })])
    quotes = pl.DataFrame({"symbol": ["600001.SH"], "close": [10.], "date": [DAY]})
    orders = []
    if grouped:
        snapshot = engine.capture_strategy_group({"stock": quotes})
        published = engine.publish_strategy_group(engine.compute_strategy_group(snapshot))
        engine.expose_strategy_group_results(published)
        events, orders = published.events, published.execution_events
    else:
        events = engine.evaluate(quotes, execution_events=orders)
    assert [(e["type"], e["symbol"]) for e in events] == [("sell_signal", "600001.SH")]
    assert not orders
    result = engine.latest_strategy_results()["custom_test"]
    assert result["section_statuses"]["buy"] != "success"
    if buy_failure:
        assert result["section_statuses"]["buy"] == "failed"
    assert result["section_statuses"]["sell"] == "success"
    assert result["exit_signal_hits"][0]["reason"] == "stop_time"
    assert result["monitor_tasks"]["sell"]["count"] == 1
