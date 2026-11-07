"""Breadth excludes unavailable members while retaining the original pool evidence."""
from datetime import timedelta

import polars as pl
import pytest
from test_late_session_strength import DAY, POOL, TARGET, bars, definition, evaluate


@pytest.mark.parametrize("missing", ["quote", "previous", "duplicate", "invalid_previous"])
def test_available_members_confirm_without_inventing_missing_returns(missing):
    frame = bars()
    previous = dict.fromkeys(POOL, 10.0)
    if missing == "quote":
        frame = frame.filter(pl.col("symbol") != "e")
    elif missing == "previous":
        previous.pop("e")
    elif missing == "duplicate":
        frame = pl.concat([frame, frame.filter(
            (pl.col("symbol") == "e") & (pl.col("datetime") == TARGET))])
    else:
        previous["e"] = float("nan")
    result = evaluate(frame=frame, prev=previous)
    assert result.symbols == POOL[:3]
    assert result.stage == "confirmed"
    assert result.pool_up_pct == 75
    assert result.pool_total == 5
    assert result.pool_usable == 4
    assert result.known_positive == 3
    assert result.missing_symbols == ["e"]
    assert result.pool_up_pct_lower == 60
    assert result.pool_up_pct_upper == 80
    assert not result.diagnostics
    assert result.warnings


def test_available_member_ratio_can_pass_when_original_lower_bound_does_not():
    result = evaluate(frame=bars().filter(pl.col("symbol") != "e"),
                      sig=definition(pool_up_min_pct=70))
    assert result.stage == "confirmed"
    assert result.symbols == POOL[:3]
    assert result.pool_up_pct == 75
    assert result.pool_up_pct_lower == 60
    assert result.pool_up_pct_upper == 80
    assert not result.diagnostics


def test_available_ratio_below_threshold_confirms_rejection_despite_unknown_quote():
    result = evaluate(frame=bars().filter(pl.col("symbol") != "e"),
                      sig=definition(pool_up_min_pct=75.01))
    assert result.stage == "breadth_rejected"
    assert result.symbols == []
    assert not result.diagnostics
    assert result.warnings
    assert result.pool_up_pct == 75


def test_missing_candidate_own_price_does_not_generate_its_signal():
    result = evaluate(frame=bars().filter(pl.col("symbol") != "a"),
                      sig=definition(pool_up_min_pct=40))
    assert result.stage == "confirmed"
    assert result.symbols == ["b", "c"]
    assert result.missing_symbols == ["a"]


def test_available_member_confirmation_still_waits_for_thirty_second_gate():
    sig = definition(evaluation_delay_minutes=.5)
    frame = bars().filter(pl.col("symbol") != "e")
    early = evaluate(frame=frame, sig=sig, completed_through=TARGET + timedelta(seconds=29))
    final = evaluate(frame=frame, sig=sig, completed_through=TARGET + timedelta(seconds=30))
    assert early.stage == "waiting_confirmation" and early.symbols == []
    assert final.stage == "confirmed" and final.symbols == POOL[:3]


def test_engine_returns_quality_without_global_error_and_resets_reused_context(tmp_path):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    custom = tmp_path / "strategies" / "custom"
    custom.mkdir(parents=True)
    (custom / "sample.py").write_text('''
import polars as pl
META = {"id": "sample", "name": "示例", "asset_types": ["stock"], "timeframes": ["1m"], "execution_backend": "minute_filter"}
BASIC_FILTER = {"enabled": False, "target_mode": "all"}
def filter_minute_history(df, params):
    return df.group_by("symbol").agg(pl.col("datetime").max().alias("last_datetime"))
''')
    engine = StrategyEngine([custom])
    custom_signals.save_one(tmp_path, definition())
    prior = pl.DataFrame({"symbol": POOL[:4], "date": [DAY-timedelta(days=1)]*4,
                          "raw_close": [10.0]*4})
    current = pl.DataFrame({"symbol": POOL, "date": [DAY]*5, "raw_close": [10.0]*5})
    context = StrategyDataContext("stock", "1m", DAY, history=bars(), pool_history=prior,
                                 current=current)
    context.intraday_data_quality["stale"] = {"stage": "waiting_data"}
    result = engine.run("sample", context, overrides={"entry_signals": ["csgi_late_session_strength"]})
    assert {hit["symbol"] for hit in result.entry_signal_hits} == set(POOL[:3])
    assert result.entry_filter_error is None
    assert "stale" not in result.entry_data_quality
    quality = result.entry_data_quality["csgi_late_session_strength"]
    assert quality["pool_total"] == 5
    assert quality["pool_usable"] == 4
    assert quality["missing_pct"] == 20
    assert quality["breadth_policy"] == "available_members_v1"
    assert quality["missing_symbols"] == ["e"]
    assert quality["pool_up_pct"] == 75
    assert quality["pool_up_pct_lower"] == 60
    assert quality["stage"] == "confirmed"
    assert quality["warnings"]


@pytest.mark.parametrize("total,missing", [(101, 1), (100, 1), (5, 1), (5, 4)])
def test_missing_percentage_never_blocks_available_member_breadth(total, missing):
    from app.strategy.late_session_strength import evaluate as calculate

    pool = [f"s{i}" for i in range(total)]
    available = pool[:total-missing]
    frame = bars().filter(pl.col("symbol") == "a").drop("symbol").join(
        pl.DataFrame({"symbol": available}), how="cross")
    result = calculate(frame, definition(pool_up_min_pct=100), day=DAY,
                       pool_symbols=pool, prev_close=dict.fromkeys(pool, 10.0))
    assert result.stage == "confirmed"
    assert result.symbols == sorted(available)
    assert result.pool_total == total
    assert result.pool_usable == total-missing
    assert result.pool_up_pct == 100
    assert not result.diagnostics
    assert pool == [f"s{i}" for i in range(total)]


def test_all_members_missing_stays_unknown_and_never_divides_by_zero():
    from app.strategy.late_session_strength import evaluate as calculate

    result = calculate(bars(), definition(pool_up_min_pct=0), day=DAY,
                       pool_symbols=POOL, prev_close={})
    assert result.stage == "waiting_data"
    assert result.pool_up_pct is None
    assert result.symbols == []
    assert result.diagnostics


@pytest.mark.parametrize("price", [None, float("nan"), float("inf"), 0.0, -1.0])
def test_unusable_target_minute_price_is_excluded_from_breadth(price):
    frame = bars().with_columns(
        pl.when((pl.col("symbol") == "e") & (pl.col("datetime") == TARGET))
        .then(pl.lit(price)).otherwise(pl.col("close")).alias("close"))
    result = evaluate(frame=frame, sig=definition(pool_up_min_pct=70))
    assert result.pool_up_pct == 75
    assert result.symbols == POOL[:3]
    assert result.missing_symbols == ["e"]


def test_cached_result_preserves_saved_breadth_range(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from test_screener_cache_api import _request

    from app.api import screener

    quality = {"csgi_late_session_strength": {
        "pool_total": 3049, "pool_up_pct": None, "pool_up_pct_lower": 76.648,
        "pool_up_pct_upper": 76.681, "missing_symbols": ["601326.SH"], "stage": "confirmed",
    }}
    monkeypatch.setattr(screener, "_history_payload", lambda *args, **kwargs: {
        "results": {"s": {"as_of": "2026-11-04", "rows": [], "entry_data_quality": quality}},
    })
    monkeypatch.setattr(screener, "_load_ext_value_maps", lambda *args: {})
    monkeypatch.setattr(screener, "_history_identities", lambda *args, **kwargs: (
        SimpleNamespace(memberships=lambda *args: {}), {}))
    result = screener.get_cached_result("s", _request(tmp_path), ext_columns=None, as_of=DAY)
    assert result["result"]["entry_data_quality"] == quality
