"""S02 breadth and frozen stock calculations must advance independently."""
from copy import deepcopy
from datetime import datetime

import pytest
from test_monitor_confirmation_retry import WINDOWS

from app.market_time import CN_TZ
from app.services.strategy_monitor_state import plan


def independent_windows():
    windows = deepcopy(WINDOWS)
    windows["buy"][0]["phase_policy"] = "independent_v1"
    return windows


@pytest.mark.parametrize("breadth", [
    {},
    {"state": "failed", "terminal": False},
    {"state": "rejected", "terminal": True},
])
def test_stock_phase_is_due_without_waiting_for_breadth(breadth):
    tasks = {"buy": {"signals": {"late": {"breadth": breadth}}}}
    actions = plan(
        independent_windows(), tasks,
        datetime(2026, 11, 23, 14, 31, tzinfo=CN_TZ), trading=True,
    )["actions"]
    assert "stocks" in [action["phase"] for action in actions]
    assert ("breadth" in [action["phase"] for action in actions]) == (
        breadth.get("state") != "rejected"
    )


def test_unfinished_breadth_remains_due_after_stock_completion():
    tasks = {"buy": {"signals": {"late": {
        "breadth": {"state": "failed", "terminal": False},
        "stocks": {"state": "success", "terminal": True},
    }}}}
    actions = plan(
        independent_windows(), tasks,
        datetime(2026, 11, 23, 14, 31, tzinfo=CN_TZ), trading=True,
    )["actions"]
    assert [action["phase"] for action in actions] == ["breadth"]


def test_undeclared_strategy_preserves_existing_phase_order():
    actions = plan(
        WINDOWS, {},
        datetime(2026, 11, 23, 14, 31, tzinfo=CN_TZ), trading=True,
    )["actions"]
    assert [action["phase"] for action in actions] == ["breadth"]


def test_stock_calculation_has_no_breadth_or_full_pool_dependency():
    import polars as pl
    from test_late_session_optional_params import AT, DAY, signal
    from test_late_session_strength import bars
    from test_s02_selected_stock_scope import selection

    from app.strategy.late_session_strength import evaluate

    state = {}
    kwargs = dict(day=DAY, pool_symbols=None, prev_close={}, task_state=state,
                  completed_through=AT.replace(second=30), selection=selection(["a"]),
                  independent_phase="stocks")
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.,
                 change_min_pct=.8, change_max_pct=4., ma5_1m_count=7, ma5_5m_count=4)
    result = evaluate(bars().filter(pl.col("symbol") == "a"), sig, **kwargs)
    assert result.symbols == ["a"] and not result.diagnostics
    assert result.pool_up_pct is None
    frozen = deepcopy(result)
    kwargs.update(pool_symbols=["unrelated"], input_error="full pool unavailable")
    assert evaluate(pl.DataFrame(), sig, **kwargs) == frozen


def test_breadth_identity_does_not_depend_on_frozen_selection():
    from test_late_session_optional_params import AT, DAY, signal
    from test_late_session_strength import POOL, bars

    from app.strategy.late_session_strength import evaluate

    state = {}
    sig = signal(time="14:30:00", evaluation_delay_minutes=.5, pool_up_min_pct=60.)
    kwargs = dict(day=DAY, pool_symbols=POOL, prev_close={s: 10. for s in POOL},
                  completed_through=AT.replace(second=15), task_state=state,
                  independent_phase="breadth")
    result = evaluate(bars(), sig, **kwargs, selection=None)
    assert result.pool_up_pct == 60. and not result.diagnostics
    assert evaluate(None, sig, **kwargs, selection={"broken": True},
                    input_error="selection unavailable") == result


@pytest.mark.parametrize("first", ["breadth", "stocks"])
def test_join_waits_for_both_phases_and_is_immutable(first):
    from app.services.strategy_monitor_state import join_buy
    now = datetime(2026, 11, 23, 14, 31, tzinfo=CN_TZ)
    stages = {
        "breadth": {"state": "success", "terminal": True, "passed": True,
                    "completed_at": now.isoformat(), "errors": []},
        "stocks": {"state": "success", "terminal": True, "passed": True,
                   "completed_at": now.isoformat(), "errors": [],
                   "rows": [{"symbol": "a", "score": 1}], "hits": [{"symbol": "a"}]},
    }
    task = {"signals": {"late": {first: stages[first]}}}
    pending, rows, hits = join_buy(task, independent_windows()["buy"], now)
    assert not pending["terminal"] and not pending["published"]
    assert rows == hits == [] and pending["count"] == 0
    pending["signals"]["late"].update(stages)
    done, rows, hits = join_buy(pending, independent_windows()["buy"], now)
    assert done["state"] == "success" and done["published"] and rows[0]["symbol"] == "a"
    assert rows[0]["entry_evaluated_at"] == now.replace(tzinfo=None).isoformat()
    assert join_buy(done, independent_windows()["buy"], now.replace(hour=15)) == (done, rows, hits)


@pytest.mark.parametrize("breadth,stocks,terminal,published,state", [
    ("failed", "success", False, False, "failed"),
    ("rejected", "failed", False, False, "failed"),
    ("rejected", "success", True, True, "rejected"),
    ("success", "rejected", True, True, "rejected"),
    ("expired", "success", True, False, "expired"),
    ("success", "cancelled", True, False, "cancelled"),
])
def test_join_preserves_failure_and_zero_result_semantics(breadth, stocks, terminal, published, state):
    from app.services.strategy_monitor_state import join_buy
    now = datetime(2026, 11, 23, 14, 31, tzinfo=CN_TZ)
    task = {"signals": {"late": {phase: {
        "state": outcome, "terminal": outcome != "failed", "passed": outcome == "success",
        "completed_at": now.isoformat(), "errors": ["missing"] if outcome == "failed" else [],
        "rows": [{"symbol": "a"}] if phase == "stocks" else [],
    } for phase, outcome in (("breadth", breadth), ("stocks", stocks))}}}
    joined, rows, _ = join_buy(task, independent_windows()["buy"], now)
    assert (joined["terminal"], joined["published"], joined["state"]) == (terminal, published, state)
    assert not rows


def test_new_join_after_deadline_cannot_publish_saved_stock_candidates():
    from app.services.strategy_monitor_state import join_buy
    now = datetime(2026, 11, 23, 14, 56, tzinfo=CN_TZ)
    success = {"state": "success", "terminal": True, "passed": True, "errors": [],
               "completed_at": now.replace(hour=14, minute=31).isoformat()}
    task = {"signals": {"late": {"breadth": success, "stocks": {
        **success, "rows": [{"symbol": "a"}], "hits": [{"symbol": "a"}]}}}}
    joined, rows, hits = join_buy(task, independent_windows()["buy"], now)
    assert joined["state"] == "expired" and not joined["published"] and not rows and not hits


def test_engine_stock_phase_uses_frozen_selection_without_loading_full_scope(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    import polars as pl
    from test_late_session_optional_params import AT, DAY
    from test_late_session_strength import bars
    from test_s02_selected_stock_scope import selection

    from app.services.screener import ScreenerService
    from app.services.strategy_monitor_tasks import run_tasks, task_windows

    # Bind display-only reference imports before patching the kernel dependency.
    from app.strategy import (
        custom_signals,
        signal_price_suggestions,  # noqa: F401
    )
    from app.strategy import late_session_strength as late
    from app.strategy.engine import StrategyEngine

    code = Path(__file__).resolve().parents[2] / "examples/strategies/s02_atr14.py"
    (tmp_path / "s02.py").write_bytes(code.read_bytes())
    engine = StrategyEngine([tmp_path])
    engine._user_data_dir = lambda: tmp_path
    sig = {**independent_windows()["buy"][0]["signal"],
           "name": "尾盘", "enabled": True, "kind": "entry", "timeframe": "intraday",
           "conditions": [], "id": "late_session_strength"}
    custom_signals.save_one(tmp_path, sig)
    sid = "custom_mvxxcp88"
    frozen = selection(["a"])
    monkeypatch.setattr(engine, "_daily_pool_loader", lambda *_: pytest.fail("stocks loaded full scope"), raising=False)
    monkeypatch.setattr(late, "previous_closes", lambda *_a, **_kw: pytest.fail("stocks loaded full references"))
    overrides = {"entry_signals": ["csgi_late_session_strength"], "exit_signals": [],
                 "basic_filter": {"data_reference": "previous_trade_day", "price_min": 4.}}
    assert engine.minute_entry_daily_history_bars([sid], overrides_map={sid: overrides}) == 0
    context = ScreenerService(SimpleNamespace(
        get_minute_by_dates=lambda symbols, dates: bars().filter(pl.col("symbol").is_in(symbols)),
    )).build_selected_stock_context(engine, DAY, sid, frozen, current=None, overrides=overrides,
                                    observed_at=AT.replace(second=30, tzinfo=CN_TZ))
    context.signal_task_states = {}
    windows = task_windows(engine, engine.get(sid), overrides)
    assert windows["buy"][0]["phase_policy"] == "independent_v1"
    pending = run_tasks(engine, engine.get(sid), context, overrides, windows, None)
    step = pending.monitor_tasks["buy"]["signals"]["late_session_strength"]["stocks"]
    assert not step["errors"], step["errors"]
    assert step["state"] == "success" and step["rows"][0]["symbol"] == "a"
    assert pending.rows == [] and pending.entry_signal_hits == []
    assert not pending.monitor_tasks["buy"]["published"]
    assert pending.section_statuses["buy"] == "waiting"


def test_compute_failure_cannot_replace_completed_independent_breadth():
    from app.multitenancy.monitor_compute import failed_phase_results
    now = datetime(2026, 11, 23, 14, 31, tzinfo=CN_TZ)
    breadth = {"state": "success", "terminal": True, "passed": True, "errors": [],
               "completed_at": now.isoformat(), "pool_up_pct": 80}
    payload = {
        "strategy_task_ids": ["s"], "strategy_task_windows": {"s": independent_windows()},
        "strategy_task_receipts": {"s": {"tasks": {"buy": {"signals": {"late": {"breadth": breadth}}}}}},
        "strategy_task_results": {}, "daily_identities": {"s": {}},
        "strategy_task_plans": {"s": {"actions": [{"kind": "buy", "signal_id": "late",
            "phase": "stocks", "scheduled_at": now.isoformat(),
            "retry_until": now.replace(minute=55).isoformat()}]}},
    }
    result = failed_phase_results(payload, "sandbox unavailable", now)["s"]
    signal = result["monitor_tasks"]["buy"]["signals"]["late"]
    assert signal["breadth"] == breadth
    assert signal["stocks"]["state"] == "failed"
    assert not result["monitor_tasks"]["buy"]["terminal"]
    assert not result["monitor_tasks"]["buy"]["published"] and result["rows"] == []


def test_commit_fence_rejects_new_join_but_retains_internal_stock_receipt():
    from app.services.strategy_monitor_tasks import apply_independent_buy, fence_tasks
    now = datetime(2026, 11, 23, 14, 54, tzinfo=CN_TZ)
    success = {"state": "success", "terminal": True, "passed": True, "errors": [],
               "completed_at": now.isoformat()}
    daily = {"monitor_contract": "strategy_monitor_v2",
             "monitor_executed_signals": {"buy": ["late"]},
             "monitor_tasks": {"buy": {"signals": {"late": {"breadth": success,
                 "stocks": {**success, "rows": [{"symbol": "a"}], "hits": [{"symbol": "a"}]}}}}}}
    apply_independent_buy(daily, independent_windows(), now)
    assert daily["rows"]
    assert fence_tasks(daily, independent_windows(), now.replace(minute=56)) == {"buy"}
    assert daily["rows"] == [] and not daily["monitor_tasks"]["buy"]["published"]
    assert daily["monitor_tasks"]["buy"]["signals"]["late"]["stocks"]["state"] == "success"
