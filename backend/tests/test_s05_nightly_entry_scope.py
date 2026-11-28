"""Exercise the archived entry cohort before minute IO and calculation."""
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from test_s05_common_daily_branch import DAY, SOURCE, common_selection, common_strategy

from app.market_time import CN_TZ
from app.services.daily_selections import DailySelections
from app.services.s05_selection import POLICY, WEIGHTS
from app.services.screener import ScreenerService
from app.services.selection_clock import S05_ID
from app.services.strategy_results import ResultIdentity
from app.strategy.engine import StrategyDataContext, StrategyEngine, StrategyResult
from app.strategy.routed_engine import RoutedStrategyEngine

SYMBOL = "600001.SH"
OUTSIDE = "600002.SH"
NOW = datetime(2026, 11, 27, 14, 15, tzinfo=CN_TZ)


def nightly(*, rows=None, clock="22:05:00", **extra):
    return {
        **common_selection(2), "identity": "s05-nightly", "scoring_policy": POLICY,
        "scoring_weights": WEIGHTS, "generated_at": f"{SOURCE}T{clock}+08:00",
        "rows": [{"symbol": SYMBOL, "reference_close": 10.}] if rows is None else rows,
        **extra,
    }


def minutes():
    return pl.DataFrame({"symbol": [SYMBOL, OUTSIDE],
                         "datetime": [NOW.replace(tzinfo=None)] * 2, "close": [10.1, 10.3]})


@pytest.mark.parametrize("stamp, extra", [
    ("23:00:00", {}), ("23:00:01", {}), ("22:05:00", {"manual_replay": {"kind": "historical"}}),
    ("22:05:00", {"rows": [{"symbol": SYMBOL}] * 2}),
    ("22:05:00", {"rows": [{"symbol": str(i)} for i in range(51)]}),
])
def test_selected_stock_loader_rejects_late_manual_or_invalid_source_before_reading(stamp, extra):
    def no_read(*args):
        pytest.fail("申报无效预选后仍查询分钟行情")

    service = ScreenerService(SimpleNamespace(get_minute_by_dates=no_read))
    engine = SimpleNamespace(get=lambda sid: common_strategy(), minute_entry_daily_history_bars=lambda *a, **k: 0)
    with pytest.raises(ValueError, match=r"S05.*预选"):
        service.build_selected_stock_context(engine, DAY, S05_ID, nightly(clock=stamp, **extra),
                                             current=None, overrides={}, observed_at=NOW)


def test_live_engine_passes_only_archived_members_to_template_and_keeps_full_pool(monkeypatch):
    from app.strategy.daily_pool import DEFINITION_VERSION

    strategy = common_strategy()
    strategy.meta.update(result_sections="signals", timeframes=["1m"])
    engine = RoutedStrategyEngine()
    engine._strategies[S05_ID] = strategy
    captured = {}

    def run(_, sid, context, *, pool, params, overrides, daily_pool, holding_context):
        captured.update(minutes=context.history["symbol"].to_list(),
                        template_scope=daily_pool["symbols"],
                        holding_input=holding_context.current["symbol"].to_list())
        return StrategyResult(DAY, sid, [], 0)

    monkeypatch.setattr(StrategyEngine, "_run_strategy", run)
    pool = {"strategy_id": S05_ID, "as_of": str(DAY), "symbols": [SYMBOL, OUTSIDE],
            "total": 2, "definition_version": DEFINITION_VERSION}
    holding = StrategyDataContext("stock", "1m", DAY, current=minutes().filter(pl.col("symbol") == OUTSIDE))
    result = engine.run(S05_ID, StrategyDataContext("stock", "1m", DAY,
        realtime=True, history=minutes(), current=minutes(), observed_at=NOW, holding_context=holding,
        selection_snapshots={S05_ID: nightly()}, daily_pool_snapshots={S05_ID: pool}))
    assert captured == {"minutes": [SYMBOL], "template_scope": [SYMBOL], "holding_input": [OUTSIDE]}
    assert result.daily_stock_pool == pool


def test_realtime_read_cannot_use_on_time_generation_saved_at_cutoff(tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.strategy_results.time.time",
                        lambda: datetime(2026, 11, 26, 23, tzinfo=CN_TZ).timestamp())
    store = DailySelections(tmp_path)
    identity = ResultIdentity(S05_ID, "s05", "saved", "{}", "{}", "null", "stock", "1m")
    store.save(identity, nightly(), started_at=1)
    assert len(store.read(S05_ID, DAY)["rows"]) == 1  # Still visible in historical results.
    assert store.read(S05_ID, DAY, live_entry=True) is None


@pytest.mark.parametrize("count", [0, 50])
def test_ordinary_live_scan_requests_exactly_the_nightly_list(monkeypatch, count):
    monkeypatch.setattr("app.market_time.cn_today", lambda: DAY)
    selected = [f"601{i:03d}.SH" for i in range(count)]
    calls = []

    def read(symbols, days):
        calls.append((symbols, days))
        return pl.DataFrame({"symbol": symbols, "datetime": [NOW.replace(tzinfo=None)] * count,
                             "close": [10.1] * count})

    service = ScreenerService(SimpleNamespace(get_minute_by_dates=read))
    engine = SimpleNamespace(get=lambda sid: common_strategy(), minute_entry_daily_history_bars=lambda *a, **k: 0)
    context = service.build_strategy_context(engine, DAY, [S05_ID], timeframe="1m",
        selection_snapshots={S05_ID: nightly(rows=[{"symbol": symbol} for symbol in selected])})
    assert calls == ([(selected, [DAY])] if count else [])
    assert context.history["symbol"].to_list() == selected
    assert context.input_errors == []


def test_empty_nightly_does_not_evaluate_any_entry_template(monkeypatch):
    strategy = common_strategy()
    engine = RoutedStrategyEngine()
    engine._strategies[S05_ID] = strategy

    def base_run(_, sid, context, pool, params, overrides):
        assert overrides["entry_signals"] == []
        return StrategyResult(DAY, sid, [], 0, section_mode="signals",
                              sections={"buy": [], "select": [], "sell": [{"symbol": OUTSIDE}]})

    monkeypatch.setattr(StrategyEngine, "run", base_run)
    result = engine.run(S05_ID, StrategyDataContext("stock", "1m", DAY, realtime=True,
                                                  selection_snapshots={S05_ID: nightly(rows=[])}))
    assert result.section_statuses["buy"] == "rejected" and result.entry_filter_error is None
    assert result.sections["sell"] == [{"symbol": OUTSIDE}]
