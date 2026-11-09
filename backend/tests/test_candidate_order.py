"""候选池默认序只读取同次候选快照, 不读实时补充行情。"""
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import cn_today
from app.strategy.candidate_order import candidate_pool_order
from app.strategy.engine import StrategyEngine, StrategyResult
from app.strategy.monitor import MonitorRuleEngine


class SnapshotEngine:
    _effective_signals = staticmethod(StrategyEngine._effective_signals)

    def __init__(self, result):
        self.result = result

    def get(self, sid):
        return SimpleNamespace(meta={"id": sid, "params": []}, filter_history_fn=None,
                               execution_backend="polars_expr", exit_signals=[])

    def run(self, sid, context, **kwargs):
        return self.result


@pytest.mark.parametrize("sid,mode,limit,expected", [
    ("custom_mvxxcp88", "signals", 5, ["000002.SZ", "600000.SH", "000001.SZ"]),
    ("other", "signals", 5, ["600000.SH", "000001.SZ", "000002.SZ"]),
    ("other", None, 1, ["000001.SZ", "600000.SH", "000002.SZ"]),
    ("other", None, 5, ["000002.SZ", "600000.SH", "000001.SZ"]),
])
def test_monitor_projects_full_default_pool_order(sid, mode, limit, expected):
    rows = [
        {"symbol": "600000.SH", "close": 10, "atr14_previous_close": 0.2,
         "return_1430": 0.2, "prev_d_amount20": 20},
        {"symbol": "000001.SZ", "close": 10, "atr14_previous_close": None,
         "return_1430": 0.3, "prev_d_amount20": 10},
        {"symbol": "000002.SZ", "close": 10, "atr14_previous_close": 0.2,
         "return_1430": 0.1, "prev_d_amount20": 30},
    ]
    original = [dict(row) for row in rows]
    result = StrategyResult(as_of=cn_today(), strategy_id=sid, rows=rows, total=3,
                            sections={"select": rows, "buy": [], "sell": []},
                            section_mode=mode, entry_signal_hits=rows)
    engine = MonitorRuleEngine()
    engine.set_strategy_engine(SnapshotEngine(result))
    engine.set_rules([{"id": "test", "type": "strategy", "strategy_id": sid,
                      "asset_type": "stock", "scope": "all",
                      "notify_events": ["buy_signal"], "cooldown_seconds": 0}])
    # Runtime params are the evaluated snapshot's defaults, not later config.
    engine._strategy_engine.get = lambda _: SimpleNamespace(
        meta={"id": sid, "params": [{"id": "position_limit", "default": limit}]},
        filter_history_fn=None, execution_backend="polars_expr", exit_signals=[])
    df = pl.DataFrame([{**row, "atr14_previous_close": 999} for row in rows])
    result.entry_signal_hits = []
    engine.evaluate(df)
    result.entry_signal_hits = rows
    events = []
    engine.evaluate(df, execution_events=events)
    assert len(events) == 3
    ordered = sorted(events, key=lambda ev: ev.get("candidate_pool_rank", 999))
    assert [ev["symbol"] for ev in ordered] == expected
    assert {ev.get("candidate_pool_rank") for ev in events} == {1, 2, 3}
    assert all(ev.get("candidate_pool_source") == {
        "strategy_id": sid, "as_of": str(cn_today()), "size": 3,
    } for ev in events)
    assert all(ev.get("candidate_pool_rule") for ev in events)
    assert rows == original


@pytest.mark.parametrize("total,symbols,errors", [
    (4, ["B", "A"], {}),
    (2, ["A", "A"], {}),
    (2, ["B", "A"], {"select": "unavailable"}),
])
def test_pool_order_refuses_incomplete_duplicate_or_failed_selection(total, symbols, errors):
    rows = [{"symbol": symbol} for symbol in symbols]
    result = StrategyResult(as_of=cn_today(), strategy_id="other", rows=rows, total=total,
                            sections={"select": rows}, section_errors=errors,
                            selection_snapshot={"total": total})
    assert candidate_pool_order(result, "other", {}) == {}


def test_grouped_proxy_preserves_full_nonsection_candidate_order_before_scope_and_display_limit():
    from app.strategy.monitor import _PrecomputedMonitorStrategyEngine
    rows = [{"symbol": symbol} for symbol in ["C", "A", "B"]]
    result = StrategyResult(as_of=cn_today(), strategy_id="other", rows=rows, total=3)
    source = SnapshotEngine(result)
    proxy = _PrecomputedMonitorStrategyEngine(source, {("stock", "other"): result},
                                             {("stock", "other"): {"display_limit": 1}})
    scoped = proxy.result_for("stock", "other", {"A", "B"})
    assert [row["symbol"] for row in scoped.rows] == ["A"]
    ranks = candidate_pool_order(scoped, "other", {})
    assert ranks["A"]["candidate_pool_rank"] == 2
    assert ranks["B"]["candidate_pool_rank"] == 3
    assert ranks["A"]["candidate_pool_source"]["size"] == 3
