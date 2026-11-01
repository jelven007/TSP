"""Single-strategy HTTP entrypoints resolve omitted execution cycles from metadata."""
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import screener, strategy
from app.services import screener as screener_service
from app.services.strategy_results import StrategyResults
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine, StrategyResult


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = StrategyEngine([])
    for sid, cycles in (("s02", ["1m"]), ("daily", ["1d"])):
        engine._strategies[sid] = StrategyDef(
            meta={"id": sid, "name": sid, "timeframes": cycles, "asset_types": ["stock"]},
            basic_filter={}, entry_signals=[], exit_signals=[], stop_loss=None,
            trailing_stop=None, trailing_take_profit_activate=None, trailing_take_profit_drawdown=None,
            max_hold_days=None, filter_fn=lambda *_: pl.lit(True), filter_history_fn=None,
            lookback_days=1, source="builtin",
        )
    contexts = []

    class Service:
        def __init__(self, repo, asset_type="stock"):
            self.asset_type = asset_type

        def build_strategy_context(self, _engine, as_of, _ids, *, timeframe="1d", **_kwargs):
            context = StrategyDataContext(self.asset_type, timeframe, as_of)
            contexts.append(context)
            return context

    def run(sid, context, **_kwargs):
        engine.validate_context(engine.get(sid), context)
        return StrategyResult(context.as_of, sid, [], 0,
                              sections={"sell": [], "select": [], "buy": []} if sid != "daily" else None)

    monkeypatch.setattr(engine, "run", run)
    monkeypatch.setattr(screener, "ScreenerService", Service)
    monkeypatch.setattr(screener_service, "ScreenerService", Service)
    monkeypatch.setattr(screener, "_load_ext_value_maps", lambda *_: {})
    monkeypatch.setattr(screener, "_coverage_warnings", lambda *_, **__: [])
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), strategy_engine=engine,
    )))
    return tmp_path, request, contexts


@pytest.mark.parametrize("entry", ["preset", "strategy"])
@pytest.mark.parametrize("sid", ["s02", "daily"])
@pytest.mark.parametrize("explicit_null", [False, True])
def test_omitted_timeframe_uses_declared_cycle_and_archives_it(runtime, entry, sid, explicit_null):
    storage, request, contexts = runtime
    values = {"strategy_id": sid, "as_of": date(2026, 10, 30)}
    if explicit_null:
        values["timeframe"] = None
    if entry == "preset":
        result = screener.run_preset(screener.PresetRequest(**values), request)
    else:
        result = strategy.run_strategy(strategy.RunRequest(**values), request)
    expected = "1d" if sid == "daily" else "1m"
    assert result["strategy_id"] == sid if "strategy_id" in result else result["strategy"] == sid
    assert contexts[-1].timeframe == expected
    records = StrategyResults(storage).list_history(sid)["records"]
    assert len(records) == 1 and records[0]["timeframe"] == expected


@pytest.mark.parametrize("entry", ["preset", "strategy"])
def test_explicit_unsupported_timeframe_is_not_silently_changed(runtime, entry):
    _, request, contexts = runtime
    values = {"strategy_id": "s02", "as_of": date(2026, 10, 30), "timeframe": "1d"}
    with pytest.raises(HTTPException, match="does not support timeframe"):
        if entry == "preset":
            screener.run_preset(screener.PresetRequest(**values), request)
        else:
            strategy.run_strategy(strategy.RunRequest(**values), request)
    assert contexts[-1].timeframe == "1d"
