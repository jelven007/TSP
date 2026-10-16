"""Keep existing signals and saved parameter units when consolidating families."""
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import build_market_data_matrix
from app.strategy.engine import StrategyEngine

BUILTIN = Path(__file__).resolve().parents[1] / "app/strategy/builtin"


def _market(ma60=None):
    close = [10.3, 9.79, 9.8, 9.9, 10.0, 10.2, 10.21, 9.7]
    frame = pl.DataFrame({
        "symbol": ["000001.SZ"] * 8,
        "date": [date(2024, 1, 2) + timedelta(days=i) for i in range(8)],
        "open": [c - 0.1 for c in close],
        "high": [c + 0.2 for c in close],
        "low": [c - 0.2 for c in close],
        "close": close,
        "volume": [1000.0] * 8,
        "ma5": [10.2, 9.5, 9.6, 10.1, 9.9, 10.1, 10.2, 9.5],
        "ma20": [10.0] * 8, "ma60": ma60 if ma60 is not None else [9.0] * 8,
        "momentum_20d": [0.02] * 8,
        "rsi_14": [40.0, 29.0, 30.0, None, 20.0, 29.0, 31.0, 25.0],
        "vol_ratio_5d": [1.0, 1.3, 1.5, 0.6, 0.7, 1.2, 0.6, 0.6],
        "change_pct": [0.02, -0.02, 0.02, 0.02, 0.011, 0.03, -0.01, 0.02],
    })
    return build_market_data_matrix(frame, field_columns={
        "ma5", "ma20", "ma60", "momentum_20d", "rsi_14", "vol_ratio_5d", "change_pct",
    })


@pytest.mark.parametrize(("sid", "entries", "exits"), [
    ("oversold_bounce", [1, 5], [1, 7]),
    ("oversold_reversal", [4, 5, 7], [1, 7]),
    ("pullback_to_support", [3, 4], [1, 7]),
    ("pullback_ma20_bounce", [3], [1, 4, 7]),
])
def test_original_variant_signals_at_thresholds_and_missing_rsi(sid, entries, exits):
    strategy = StrategyEngine._load_file(BUILTIN / f"{sid}.py")
    signals = strategy.matrix_strategy.compute_signals(_market(), {})
    assert np.flatnonzero(signals.entry).tolist() == entries
    assert np.flatnonzero(signals.exit).tolist() == exits


def test_saved_proximity_units_and_disabled_filters_remain_compatible():
    support = StrategyEngine._load_file(BUILTIN / "pullback_to_support.py")
    bounce = StrategyEngine._load_file(BUILTIN / "pullback_ma20_bounce.py")
    market = _market()
    a = support.matrix_strategy.compute_signals(market, {
        "ma_proximity": 0.05, "use_volume_filter": False,
        "require_above_ma60": False, "require_positive_momentum": False,
    })
    b = bounce.matrix_strategy.compute_signals(market, {
        "ma_proximity": 5.0, "require_ma_alignment": False, "require_positive_change": False,
    })
    np.testing.assert_array_equal(a.entry, b.entry)
    assert a.entry.all()
    assert support.max_hold_days == 20
    assert bounce.max_hold_days == 15
    assert support.meta["asset_types"] == ["stock", "etf"]
    assert bounce.meta["asset_types"] == ["stock"]
    assert len(support.exit_signals) == 1
    assert len(bounce.exit_signals) == 2


@pytest.mark.parametrize("sid", ["oversold_bounce", "oversold_reversal"])
def test_disabling_rsi_filter_preserves_unknown_rsi_behavior(sid):
    strategy = StrategyEngine._load_file(BUILTIN / f"{sid}.py")
    signals = strategy.matrix_strategy.compute_signals(_market(), {
        "use_rsi_filter": False, "use_volume_filter": False,
        "require_bullish_candle": False, "use_change_filter": False, "require_above_ma5": False,
    })
    assert signals.entry.all()


def test_ma60_non_declining_filter_can_restore_original_entry_behavior():
    strategy = StrategyEngine._load_file(BUILTIN / "ma_golden_cross.py")
    market = _market(ma60=[10.0, 10.0, 10.0, 9.0, 9.0, 9.0, 10.0, 11.0])
    params = {
        "require_ma_golden": False, "use_volume_filter": False, "require_above_ma60": False,
    }
    guarded = strategy.matrix_strategy.compute_signals(market, params)
    assert np.flatnonzero(guarded.entry).tolist() == [6, 7]
    original = strategy.matrix_strategy.compute_signals(
        market, {**params, "require_ma60_non_declining": False},
    )
    assert original.entry.all()
    np.testing.assert_array_equal(guarded.exit, original.exit)
    assert strategy.matrix_strategy.required_warmup_bars({}) == 65
    assert strategy.matrix_strategy.required_warmup_bars({"require_ma60_non_declining": False}) == 60


def test_catalog_groups_exactly_22_defaults_and_excludes_research_template():
    from app.strategy.families import BUILTIN_FAMILIES, builtin_family

    engine = StrategyEngine([BUILTIN])
    ids = [sid for _, members in BUILTIN_FAMILIES.values() for sid in members]
    assert len(ids) == len(set(ids)) == 22
    assert set(ids) == {s["id"] for s in engine.list_strategies()}
    assert builtin_family("factor_rank_research") is None
    assert builtin_family("custom_unknown") is None
    assert len(BUILTIN_FAMILIES) == 6


def test_api_family_contract_keeps_ids_overrides_and_uncategorized_strategies(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import strategy as api

    engine = StrategyEngine([BUILTIN])
    monkeypatch.setattr(api, "_get_engine", lambda request: engine)
    monkeypatch.setattr(api, "_data_dir", lambda request: tmp_path)
    monkeypatch.setattr(api.strategy_config, "list_overrides", lambda data: {
        "ma_golden_cross": {"params": {"require_ma60_non_declining": False}},
    })
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        response = client.get("/api/strategies")
    assert response.status_code == 200
    items = {s["id"]: s for s in response.json()["strategies"]}
    assert len(items) == 22
    assert items["ma_golden_cross"]["family"] == {"id": "trend", "name": "趋势跟随"}
    assert items["ma_golden_cross"]["params_defaults"]["require_ma60_non_declining"] is False
    custom = engine.get("oversold_bounce")
    custom.source = "custom"
    assert api._strategy_detail(custom)["family"] is None
