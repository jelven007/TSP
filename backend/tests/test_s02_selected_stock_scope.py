"""The full breadth identity survives while only frozen members enter stock math."""
from datetime import timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from test_late_session_optional_params import AT, DAY, signal
from test_late_session_strength import POOL, bars

from app.strategy import late_session_strength as late


def selection(symbols):
    return {"as_of": str(DAY), "source_date": "2026-09-03",
            "rows": [{"symbol": symbol, "reference_close": 10.} for symbol in symbols]}


def run(state, frame, selected, *, seconds=30):
    return late.evaluate(
        frame, signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.,
                      change_min_pct=.8, change_max_pct=4., ma5_1m_count=7, ma5_5m_count=4),
        day=DAY, pool_symbols=POOL, prev_close={s: 10. for s in POOL},
        completed_through=AT + timedelta(seconds=seconds), task_state=state,
        selection=selected, diagnostic_symbols=[r["symbol"] for r in selected["rows"]],
    )


def test_full_breadth_then_only_selected_members_reach_stock_math(monkeypatch):
    state = {}
    frozen = selection(["a", "d"])
    breadth = run(state, bars(), frozen, seconds=15)
    assert breadth.pool_total == 5 and breadth.pool_up_pct == 60.
    identity = state["identity"]
    calls = []
    original = late._ready_symbols

    def ready(frame, symbols, *args):
        calls.append((set(frame["symbol"]), set(symbols)))
        return original(frame, symbols, *args)

    monkeypatch.setattr(late, "_ready_symbols", ready)
    final = run(state, bars(), frozen)
    assert final.symbols == ["a"]
    assert all(members <= {"a", "d"} and candidates <= {"a", "d"}
               for members, candidates in calls)
    assert final.pool_total == 5 and final.pool_up_pct == 60.
    assert state["identity"] == identity


def test_completed_breadth_accepts_only_selected_minutes_and_references():
    state = {}
    frozen = selection(["a"])
    run(state, bars(), frozen, seconds=15)
    final = run(state, bars().filter(pl.col("symbol") == "a"), frozen)
    assert final.symbols == ["a"] and not final.diagnostics
    assert final.pool_total == 5 and final.pool_usable == 5


def test_empty_frozen_selection_needs_no_stock_minutes_or_ma(monkeypatch):
    state = {}
    frozen = selection([])
    run(state, bars(), frozen, seconds=15)
    monkeypatch.setattr(late, "_ready_symbols",
                        lambda *args: (_ for _ in ()).throw(AssertionError("empty selection evaluated MA")))
    final = run(state, pl.DataFrame(), frozen)
    assert final.stage == "conditions_rejected" and final.symbols == []
    assert not final.diagnostics
    assert final.pool_total == 5 and final.pool_up_pct == 60.


def test_selected_reference_is_frozen_instead_of_reloading_unrelated_daily(tmp_path, monkeypatch):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    sig = {"id": "late", "name": "尾盘走强", "kind": "entry", "conditions": [],
           "timeframe": "intraday",
           **signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.,
                    change_min_pct=.8)}
    custom_signals.save_one(tmp_path, sig)
    state = {}
    frozen = selection(["a"])
    # 用相同信号冻结完整广度, 然后模拟个股计算上下文。
    late.evaluate(bars(), sig, day=DAY, pool_symbols=POOL, prev_close={s: 10. for s in POOL},
                  completed_through=AT + timedelta(seconds=15), task_state=state, breadth_only=True)
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    monkeypatch.setattr(late, "previous_closes",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("stock task reloaded daily")))
    context = StrategyDataContext("stock", "1m", DAY, observed_at=AT + timedelta(seconds=30),
                                  signal_task_scope="sample", signal_task_states={"sample:csgi_late": state})
    result = engine._inject_intraday_signal_columns(
        bars().filter(pl.col("symbol") == "a"), context=context, pool_symbols=POOL,
        selection=frozen, diagnostic_symbols=["a"])
    assert not context.intraday_errors
    assert result["csgi_late"].any()


@pytest.mark.parametrize("members", [["a", "d"], []])
def test_stock_context_reads_exact_frozen_members_and_never_loads_full_daily(members):
    from app.services.screener import ScreenerService

    reads = []
    def minutes(symbols, dates):
        reads.append((symbols, dates))
        return bars().filter(pl.col("symbol").is_in(symbols))
    repo = SimpleNamespace(get_minute_by_dates=minutes)
    engine = SimpleNamespace(minute_entry_daily_history_bars=lambda *args, **kw: 0,
                             get=lambda _sid: SimpleNamespace(meta={}))
    context = ScreenerService(repo).build_selected_stock_context(
        engine, DAY, "custom_mvxxcp88", selection(members),
        current=bars().select("symbol").unique(), overrides={},
        observed_at=AT + timedelta(seconds=30))
    assert reads == ([(members, [DAY])] if members else [])
    assert context.selection_snapshots["custom_mvxxcp88"]["rows"] == selection(members)["rows"]
    assert set(context.current["symbol"]) == set(members)
    assert context.daily_history is None and context.pool_history is None


def test_stock_context_rejects_missing_or_wrong_day_selection_without_any_io():
    from app.services.screener import ScreenerService

    service = ScreenerService(SimpleNamespace())
    for frozen in (None, {**selection(["a"]), "as_of": "2026-09-03"},
                   {**selection(["a"]), "source_date": "2026-09-02"}):
        with pytest.raises(ValueError, match="预选"):
            service.build_selected_stock_context(
                SimpleNamespace(), DAY, "custom_mvxxcp88", frozen,
                current=None, overrides={}, observed_at=AT)


def test_stock_repairs_use_preselection_while_breadth_repairs_keep_complete_pool(monkeypatch):
    from app.services import strategy_monitor_tasks as tasks

    requests = []
    monkeypatch.setattr(tasks, "request_input_repair", lambda market, **kw:
                        (requests.append(kw), {"state": "queued"})[1])
    phase = {"state": "retrying", "errors": ["missing"], "attempts": 1,
             "retry_until": (AT + timedelta(minutes=25)).isoformat()}
    payload = {"tenant_id": "tenant", "strategy_task_generations": {"s": 1},
               "strategy_task_windows": {"s": {"buy": [{"start": "14:30:00", "signal": {"id": "late"}}]}},
               "daily_pool_snapshots": {"s": {"symbols": POOL}},
               "selection_snapshots": {"s": selection(["a"])}}
    results = {"s": {"monitor_tasks": {"buy": {"signals": {"late": {
        "breadth": dict(phase), "stocks": dict(phase)}}}}}}
    tasks.request_phase_repairs(payload, results, None, AT)
    assert [(r["phase"], r["symbols"]) for r in requests] == [("breadth", POOL), ("stocks", ["a"])]
