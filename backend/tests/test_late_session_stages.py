"""The early breadth stage never evaluates MA5 or confirms entries."""
from datetime import timedelta

import polars as pl
from test_late_session_strength import POOL, TARGET, bars, definition, evaluate


def test_fifteen_seconds_produces_full_breadth_and_candidates_without_ma(monkeypatch):
    from app.strategy import late_session_strength as late
    monkeypatch.setattr(late, "_rising_symbols",
                        lambda *args: (_ for _ in ()).throw(AssertionError("early MA5")))
    sig = definition(evaluation_delay_minutes=.5)
    early = evaluate(sig=sig, completed_through=TARGET + timedelta(seconds=14))
    assert early.pool_up_pct is None
    staged = evaluate(sig=sig, completed_through=TARGET + timedelta(seconds=15))
    assert staged.pool_up_pct == 60
    assert staged.candidates == POOL[:3]
    assert staged.symbols == []
    assert staged.stage == "waiting_confirmation"


def test_confirmation_recomputes_revised_breadth_instead_of_reusing_old_candidates():
    sig = definition(evaluation_delay_minutes=.5)
    staged = evaluate(sig=sig, completed_through=TARGET + timedelta(seconds=15))
    assert staged.pool_up_pct == 60
    revised = bars().with_columns(
        pl.when(pl.col("symbol") == "c").then(9.).otherwise(pl.col("close")).alias("close"))
    final = evaluate(frame=revised, sig=sig, completed_through=TARGET + timedelta(seconds=30))
    assert final.symbols == []
    assert final.pool_up_pct == 40
    assert final.stage == "breadth_rejected"


def test_missing_pool_prices_are_excluded_without_waiting():
    sig = definition(evaluation_delay_minutes=.5, pool_up_min_pct=60.01)
    result = evaluate(frame=bars().filter(pl.col("symbol") != "e"), sig=sig,
                      completed_through=TARGET + timedelta(seconds=30))
    assert result.stage == "confirmed"
    assert result.pool_up_pct == 75
    assert not result.diagnostics


def test_missing_candidate_ma_history_skips_stock_and_confirms_remaining_entries():
    frame = bars().filter(~((pl.col("symbol") == "a")
                            & (pl.col("datetime") == TARGET - timedelta(minutes=7))))
    result = evaluate(frame=frame, sig=definition(evaluation_delay_minutes=.5),
                      completed_through=TARGET + timedelta(seconds=30))
    assert result.stage == "confirmed"
    assert result.symbols == ["b", "c"]
    assert not result.diagnostics
    assert result.ma_skipped_symbols == ["a"]
    assert result.warnings


def test_injection_exposes_excluded_full_pool_as_quality_warning(tmp_path):
    from test_late_session_strength import DAY

    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine
    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, definition(evaluation_delay_minutes=.5, pool_up_min_pct=60.01))
    previous = pl.DataFrame({"symbol": POOL, "date": [DAY - timedelta(days=1)] * len(POOL),
                             "raw_close": [10.] * len(POOL)})
    context = StrategyDataContext("stock", "1m", DAY, pool_history=previous,
                                  observed_at=TARGET + timedelta(seconds=30))
    frame = bars().filter(pl.col("symbol") != "e")
    engine._inject_intraday_signal_columns(frame, context=context, pool_symbols=POOL)
    assert not context.intraday_errors
    quality = context.intraday_data_quality["csgi_late_session_strength"]
    assert quality["missing_symbols"] == ["e"]
    assert quality["pool_up_pct"] == 75
    assert quality["warnings"]
