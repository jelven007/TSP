"""Economic definitions, chronology, missing bars, and original-policy compatibility."""

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.backtest.matrix import (
    MatrixPipelineConfig,
    MatrixStrategyPipeline,
    RealtimeMarketDataMatrix,
    build_market_data_matrix,
    validate_signal_matrix,
)
from app.backtest.strategy import StrategyDependencyResolver
from app.strategy.builtin._quality_entries import (
    VARIANTS,
    confirm_frozen_high,
    first_event,
)
from app.strategy.engine import StrategyEngine

BUILTIN = Path(__file__).resolve().parents[2] / "app/strategy/builtin"


def strategy(sid):
    return StrategyEngine._load_file(BUILTIN / f"{sid}.py").matrix_strategy


def panel(n=100, **columns):
    t = np.arange(n)
    close = 10 + np.sin(t / 3) * .8 + t * .004
    values = {
        "date": [date(2024, 1, 2) + timedelta(days=int(i)) for i in t],
        "symbol": ["000001.SZ"] * n, "name": ["测试"] * n,
        "open": close - .1, "high": close + .2, "low": close - .3, "close": close,
        "raw_close": close, "volume": np.full(n, 10000.), "amount": np.full(n, 1e8),
        "ma5": close - .2, "ma10": close - .3, "ma20": np.full(n, 10.),
        "ma60": np.full(n, 9.), "low_60d": close + .1, "high_60d": close - .1,
        "momentum_5d": np.full(n, -.06), "momentum_20d": np.full(n, .06),
        "momentum_60d": np.full(n, .08), "turnover_rate": np.full(n, 8.),
        "vol_ratio_5d": 2 + np.sin(t), "change_pct": np.full(n, .04),
        "rsi_14": 40 + 30 * np.sin(t / 4), "macd_dif": -.1 + np.sin(t / 6) * .2,
        "annual_vol_20d": np.full(n, .2), "boll_upper": np.full(n, 10.5),
        "boll_lower": np.full(n, 9.), "atr_14": np.full(n, .4),
        "consecutive_limit_ups": np.where(t % 4 == 0, 2, 0),
        "price_limit_pct": np.full(n, .1), "signal_limit_up": t % 4 == 0,
        "limit_up_count_60d": np.full(n, 4.), "turnover_ratio_5d": np.full(n, 1.),
        "close_position": np.full(n, .6), "up_days_20d": np.full(n, 10.),
        "prev_close": close + .1,
    }
    for key, value in columns.items():
        values[key] = [value] * n if np.isscalar(value) else value
    return pl.DataFrame(values, strict=False)


def market(frame):
    return build_market_data_matrix(frame, field_columns=set(frame.columns) - {"date", "symbol", "name"})


def run(sid, variant, frame, **params):
    return strategy(sid).compute_signals(market(frame), {**params, "entry_variant": variant})


@pytest.mark.parametrize("sid", VARIANTS)
def test_original_is_exact_delegate_and_old_warmup_is_preserved(sid):
    wrapped = strategy(sid)
    data = market(panel())
    a = wrapped.compute_signals(data, {})
    b = wrapped.base.compute_signals(data, {})
    for field in ("entry", "exit", "score", "entry_signal_code", "exit_signal_code"):
        np.testing.assert_array_equal(getattr(a, field), getattr(b, field))
    assert wrapped.required_warmup_bars({}) == wrapped.base.required_warmup_bars({})
    assert wrapped.required_fields_for_params({}) == frozenset()
    with pytest.raises(ValueError, match="unknown entry_variant"):
        wrapped.compute_signals(data, {"entry_variant": "typo"})


CASES = [(sid, item.id) for sid, variants in VARIANTS.items() for item in variants]


@pytest.mark.parametrize(("sid", "variant"), CASES)
def test_each_candidate_is_prefix_causal_retains_exits_and_rejects_nontrading_bars(sid, variant):
    wrapped = strategy(sid)
    frame = panel()
    data = market(frame)
    result = wrapped.compute_signals(data, {"entry_variant": variant})
    validate_signal_matrix(result, data.shape)
    original = wrapped.base.compute_signals(data, {})
    np.testing.assert_array_equal(result.exit, original.exit)
    np.testing.assert_array_equal(result.exit_signal_code, original.exit_signal_code)
    assert result.exit_signal_ids == original.exit_signal_ids
    for end in (35, 60, 80):
        prefix = wrapped.compute_signals(market(frame.head(end)), {"entry_variant": variant})
        np.testing.assert_array_equal(prefix.entry, result.entry[:end])
    dead = market(frame.with_columns(pl.lit(0.).alias("volume")))
    assert not wrapped.compute_signals(dead, {"entry_variant": variant}).entry.any()


def test_catalog_has_at_most_two_hypotheses_and_defaults_remain_original():
    engine = StrategyEngine([BUILTIN])
    assert len(engine.list_strategies()) == 22
    for sid, variants in VARIANTS.items():
        assert 1 <= len(variants) <= 2
        loaded = engine.get(sid)
        param = next(p for p in loaded.meta["params"] if p["id"] == "entry_variant")
        assert param["default"] == "original"
        assert set(param["options"]) == {"original", *(v.id for v in variants)}
        for variant in variants:
            columns = engine._matrix_field_columns(loaded, None, {"entry_variant": variant.id})
            # Base OHLCV is loaded regardless of optional feature projection.
            if "atr_14" in variant.fields:
                assert "atr_14" in columns
                plan = StrategyDependencyResolver().resolve(
                    loaded, params={"entry_variant": variant.id},
                    basic_filter=loaded.basic_filter, entry_signals=loaded.entry_signals,
                    exit_signals=loaded.exit_signals,
                )
                assert {"high", "low", "close"} <= plan.base_columns


def test_first_event_requires_observed_reset_and_carries_state_over_suspension():
    state = np.array([0, 1, 1, 0, 1, 0, 1], bool)[:, None]
    observed = np.array([1, 1, 1, 0, 1, 1, 1], bool)[:, None]
    assert np.flatnonzero(first_event(state, observed)).tolist() == [1, 6]


@pytest.mark.parametrize(("days", "expected"), [(1, []), (3, [3])])
def test_two_stage_confirmation_freezes_shape_and_does_not_require_new_low_again(days, expected):
    frame = panel(5, close=[10, 9, 9.2, 9.7, 10], open=[9.8, 8.9, 9, 9.5, 9.9],
                  high=[10.2, 9.5, 9.6, 9.9, 10.2], low=[9.7, 8.8, 9, 9.3, 9.8])
    seed = np.array([0, 1, 0, 0, 0], bool)[:, None]
    entries, dates = confirm_frozen_high(market(frame), seed, days)
    assert np.flatnonzero(entries).tolist() == expected
    if expected:
        assert dates[3, 0] == 1


def test_two_stage_low_breach_wins_over_high_break_and_missing_days_expire():
    frame = panel(6, close=[10, 9, 10, 10, 10, 10],
                  high=[10.2, 9.5, 10.2, 10.2, 10.2, 10.2],
                  low=[9.7, 8.8, 8.7, 9.8, 9.8, 9.8])
    seed = np.array([0, 1, 0, 0, 0, 0], bool)[:, None]
    assert not confirm_frozen_high(market(frame), seed, 3)[0].any()
    suspended = frame.with_columns(
        pl.Series("volume", [10000., 10000., 0., 0., 0., 10000.]),
        pl.Series("low", [9.7, 8.8, 9., 9., 9., 9.8]),
    )
    assert not confirm_frozen_high(market(suspended), seed, 3)[0].any()


def test_new_low_delayed_rule_confirms_above_frozen_high_and_exit_still_has_priority():
    frame = panel(5, close=[10, 9, 9.2, 9.7, 10],
                  open=[9.8, 8.9, 9.1, 9.5, 9.9], high=[10.2, 9.5, 9.6, 9.9, 10.2],
                  low=[9.7, 8.8, 9, 9.3, 9.8], low_60d=[9.5, 9, 9, 9, 9],
                  ma20=[9, 8, 8, 10, 10], vol_ratio_5d=2.)
    signals = run("n_day_low_reversal", "confirm_high_3", frame)
    assert np.flatnonzero(signals.entry).tolist() == [3]
    assert signals.exit[3, 0] == 1
    filtered = MatrixStrategyPipeline().run(
        strategy("n_day_low_reversal"), market(frame), {"entry_variant": "confirm_high_3"},
        MatrixPipelineConfig(basic_filter={}, scoring={}, order_by="score", descending=True),
    )
    assert not filtered.entry[3, 0]


@pytest.mark.parametrize(("variant", "expected"), [
    ("ma20_atr_15", [0, 1]), ("ma20_atr_20", [0, 1, 2]),
])
def test_atr_distance_boundaries_and_zero_atr(variant, expected):
    frame = panel(4, close=[10.5, 10.75, 11, 10.1], ma20=10., atr_14=[.5, .5, .5, 0])
    params = {"require_ma_golden": False, "use_volume_filter": False,
              "require_above_ma60": False, "require_ma60_non_declining": False}
    assert np.flatnonzero(run("ma_golden_cross", variant, frame, **params).entry).tolist() == expected


def test_macd_zero_axis_and_local_two_day_rise():
    frame = panel(5, macd_dif=[-.3, -.2, -.1, 0, .1])
    params = {"require_macd_golden": False, "use_volume_filter": False}
    above = run("macd_golden", "dif_nonnegative", frame, **params)
    below = run("macd_golden", "dif_negative", frame, **params)
    assert np.flatnonzero(above.entry).tolist() == [3, 4]
    assert np.flatnonzero(below.entry).tolist() == [0, 1, 2]
    frame = panel(15, close=np.arange(15, 0, -1, dtype=float), macd_dif=np.arange(-15, 0, dtype=float))
    result = run("macd_below_zero_revival", "dif_rising_2", frame, low_window=5, revive_days=3)
    assert np.flatnonzero(result.entry).tolist() == list(range(3, 15))


def test_platform_definition_uses_only_prior_price_level_and_complete_window():
    frame = panel(6, close=[10, 10, 10, 10, 10, 12], high=[10.5] * 5 + [12.2],
                  low=[9.5] * 5 + [11.8], vol_ratio_5d=2.)
    original = run("platform_consolidation_breakout", "original", frame, platform_days=5, range_pct_max=9)
    candidate = run("platform_consolidation_breakout", "prior_platform_mean", frame,
                    platform_days=5, range_pct_max=9)
    assert original.entry[-1, 0] == 1  # old denominator 12 makes 8.33%
    assert not candidate.entry.any()  # historical platform itself was 10%
    assert run("platform_consolidation_breakout", "prior_platform_mean", frame,
               platform_days=5, range_pct_max=10).entry[-1, 0] == 1


def test_platform_volume_topic_is_independent_of_denominator_topic():
    frame = panel(6, close=[10] * 5 + [12], high=[10.5] * 5 + [12.2],
                  low=[9.5] * 5 + [11.8], volume=[300., 300., 200., 100., 100., 500.],
                  vol_ratio_5d=2.)
    assert run("platform_consolidation_breakout", "platform_volume_contract", frame,
               platform_days=5, range_pct_max=9).entry[-1, 0] == 1


@pytest.mark.parametrize("variant", ["prior_platform_mean", "platform_volume_contract"])
def test_platform_columns_share_the_same_complete_bar_dates(variant):
    frame = panel(7, close=[10.] * 6 + [12.],
                  high=[10.5] * 5 + [None, 12.2], low=[9.5] * 5 + [1., 11.8],
                  volume=[300., 300., 200., 100., 100., 9000., 500.], vol_ratio_5d=2.)
    result = run("platform_consolidation_breakout", variant, frame,
                 platform_days=5, range_pct_max=10)
    assert result.entry[-1, 0] == 1  # Skip the incomplete row for every column.
    short = run("platform_consolidation_breakout", variant, frame.slice(1),
                platform_days=5, range_pct_max=10)
    assert not short.entry.any()  # Only four prior complete bars remain.


def test_pullback_cycle_is_first_only_and_has_market_day_expiry():
    frame = panel(16, close=[9.9] + [10.1] * 15, ma5=10.2, ma20=10., ma60=9.,
                  change_pct=.01)
    result = run("pullback_ma20_bounce", "first_pullback_10", frame)
    assert np.flatnonzero(result.entry).tolist() == [2]
    frame = frame.with_columns(pl.Series("change_pct", [-.01] * 12 + [.01] * 4))
    assert not run("pullback_ma20_bounce", "first_pullback_10", frame).entry.any()


def test_rsi_recent_oversold_replaces_same_day_gate_but_preserves_other_confirmation():
    frame = panel(6, rsi_14=[40, 25, 32, 35, 40, 40], close=10., ma5=9.,
                  change_pct=[.02, .02, .02, -.02, .02, .02], vol_ratio_5d=2.)
    assert np.flatnonzero(run("oversold_reversal", "recent_oversold_3", frame).entry).tolist() == [2, 4]
    assert np.flatnonzero(run("oversold_bounce", "rsi_recover", frame, require_bullish_candle=False).entry).tolist() == [2]


@pytest.mark.parametrize(("sid", "variant", "values", "expected", "field", "params"), [
    ("volume_price_surge", "volume_cap_4", [3.9, 4., 4.1], [0, 1], "vol_ratio_5d",
     {"require_ma20_breakout": False, "require_bullish_candle": False}),
    ("high_turnover_surge", "turnover_cap_15", [5., 15., 15.1], [1], "turnover_rate", {}),
    ("rsi_midline_pullback", "rsi_turn_up", [70., 49., 50.], [2], "rsi_14", {}),
    ("rsi_midline_pullback", "rsi_cross_50", [70., 50., 51.], [2], "rsi_14", {}),
])
def test_filter_boundaries(sid, variant, values, expected, field, params):
    assert np.flatnonzero(run(sid, variant, panel(3, **{field: values}), **params).entry).tolist() == expected


def test_clv_uses_actual_range_rejects_flat_and_missing_bars():
    frame = panel(4, close=[10.65, 10.64, 10., 10.8], open=10., high=[11., 11., 10., None],
                  low=10., low_60d=11., vol_ratio_5d=2.)
    result = run("n_day_low_reversal", "clv_065", frame)
    assert np.flatnonzero(result.entry).tolist() == [0]


def test_daily_limit_split_is_exhaustive_and_gene_recency_uses_market_sessions():
    frame = panel(24, change_pct=.08, signal_limit_up=[True] + [False] * 23,
                  vol_ratio_5d=1.)
    closed = run("near_limit_up", "closed_limit", frame)
    opened = run("near_limit_up", "not_closed_limit", frame)
    assert np.flatnonzero(closed.entry).tolist() == [0]
    assert np.flatnonzero(opened.entry).tolist() == list(range(1, 24))
    gene = run("active_limit_gene", "recent_limit_20", frame, max_change_pct=9)
    assert np.flatnonzero(gene.entry).tolist() == list(range(1, 21))


def test_continuous_squeeze_requires_fresh_full_squeeze_before_rearming():
    frame = panel(14, close=[9.95] * 3 + [10.5] * 5 + [9.95] * 3 + [10.5] * 3,
                  ma5=10., ma10=9.9, ma20=9.95, vol_ratio_5d=2.)
    result = run("ma_convergence_breakout", "first_squeeze_release", frame, squeeze_days=3)
    assert np.flatnonzero(result.entry).tolist() == [3, 11]


def test_below_band_prices_do_not_establish_in_band_squeeze():
    frame = panel(7, close=[9.8] * 3 + [10.5] * 4,
                  ma5=10., ma10=9.9, ma20=9.95, vol_ratio_5d=2.)
    original = run("ma_convergence_breakout", "original", frame, squeeze_days=3)
    assert original.entry[3, 0]
    candidate = run("ma_convergence_breakout", "first_squeeze_release", frame, squeeze_days=3)
    assert not candidate.entry.any()


@pytest.mark.parametrize(("sid", "variant"), [
    ("n_day_low_reversal", "confirm_high_3"),
    ("pullback_ma20_bounce", "first_pullback_10"),
    ("ma_convergence_breakout", "first_squeeze_release"),
])
def test_live_snapshot_update_and_batch_share_same_signal_policy(sid, variant):
    frame = panel()
    live = RealtimeMarketDataMatrix(
        frame.head(99), field_columns=set(frame.columns) - {"date", "symbol", "name"},
    )
    live.update(frame.tail(1))
    pipeline = MatrixStrategyPipeline()
    config = MatrixPipelineConfig(basic_filter={}, scoring={}, order_by="score", descending=True)
    impl = strategy(sid)
    a = pipeline.run(impl, live.snapshot(), {"entry_variant": variant}, config)
    b = pipeline.run(impl, market(frame), {"entry_variant": variant}, config)
    np.testing.assert_array_equal(a.entry, b.entry)
    np.testing.assert_array_equal(a.exit, b.exit)
    # Repeated replacement of today's row must not advance the observation age.
    live.update(frame.tail(1))
    c = pipeline.run(impl, live.snapshot(), {"entry_variant": variant}, config)
    np.testing.assert_array_equal(a.entry, c.entry)
