"""Stage receipts must explain elimination without changing entry membership."""
from datetime import timedelta

import polars as pl
import pytest
from test_late_session_strength import DAY, POOL, TARGET, bars, definition, evaluate


def test_progress_waits_for_saved_prefilter_and_confirmation_times():
    sig = definition(evaluation_delay_minutes=.5)
    early = evaluate(sig=sig, completed_through=TARGET + timedelta(seconds=14))
    progress = early.progress
    assert progress["schema"] == "late_session_progress_v1"
    assert progress["steps"]["breadth"]["scheduled_at"] == "2026-09-04T14:30:15"
    assert progress["steps"]["ma1"]["scheduled_at"] == "2026-09-04T14:30:30"
    assert all(step["state"] == "waiting" and step["processed"] is None
               and step["started_at"] is None for step in progress["steps"].values())
    staged = evaluate(sig=sig, completed_through=TARGET + timedelta(seconds=15))
    assert staged.progress["steps"]["change"]["passed"] == 3
    assert staged.progress["steps"]["ma1"]["state"] == "waiting"


def test_breadth_rejection_still_records_change_count_and_never_runs_ma(monkeypatch):
    from app.strategy import late_session_strength as late

    monkeypatch.setattr(late, "_rising_symbols",
                        lambda *args: pytest.fail("rejected breadth evaluated MA"))
    result = evaluate(sig=definition(pool_up_min_pct=61, evaluation_delay_minutes=.5),
                      completed_through=TARGET + timedelta(seconds=30),
                      diagnostic_symbols=["a", "d"])
    steps = result.progress["steps"]
    assert result.symbols == [] and result.candidates == []
    assert steps["breadth"]["state"] == "rejected"
    assert steps["breadth"]["processed"] == 5
    assert steps["breadth"]["passed"] == 3
    assert steps["change"]["pool_passed"] == 3
    assert steps["change"]["total"] == 2 and steps["change"]["passed"] == 1
    assert all(steps[key]["state"] == "skipped" and steps[key]["processed"] is None
               for key in ("ma1", "ma5"))


def test_ma_receipts_distinguish_one_and_five_minute_elimination():
    frame = bars().with_columns(
        pl.when(pl.col("symbol") == "a").then(10.15)
        .when((pl.col("symbol") == "b") & (pl.col("datetime") < TARGET - timedelta(minutes=10)))
        .then(10.3).otherwise(pl.col("close")).alias("close"))
    result = evaluate(frame=frame, diagnostic_symbols=POOL[:3])
    steps = result.progress["steps"]
    assert steps["ma1"]["total"] == 3 and steps["ma1"]["processed"] == 3
    assert steps["ma1"]["passed"] == 2 and steps["ma1"]["missing"] == 0
    assert steps["ma5"]["total"] == 2 and steps["ma5"]["processed"] == 2
    assert steps["ma5"]["passed"] == 1 and steps["ma5"]["missing"] == 0
    assert result.symbols == ["c"]
    assert steps["ma1"]["started_at"] and steps["ma5"]["finished_at"]
    assert steps["ma1"]["elapsed_ms"] >= 0


def test_missing_five_minute_window_is_separate_from_ma_decline():
    frame = bars().filter(~((pl.col("symbol") == "a")
                            & (pl.col("datetime") == TARGET - timedelta(minutes=20))))
    result = evaluate(frame=frame, diagnostic_symbols=POOL[:3])
    assert result.progress["steps"]["ma1"]["passed"] == 3
    assert result.progress["steps"]["ma5"]["total"] == 3
    assert result.progress["steps"]["ma5"]["missing"] == 1
    assert result.progress["steps"]["ma5"]["passed"] == 2
    assert result.ma_skipped_symbols == ["a"] and result.symbols == ["b", "c"]


def test_diagnostic_subset_cannot_shrink_pool_or_change_signal():
    result = evaluate(diagnostic_symbols=["a", "d", "outside"])
    assert result.pool_total == 5 and result.pool_up_pct == 60
    assert result.symbols == POOL[:3]
    assert result.progress["cohort"] == "frozen_selection"
    assert result.progress["steps"]["change"]["total"] == 2
    assert result.progress["steps"]["ma1"]["total"] == 1
    assert evaluate(diagnostic_symbols=[]).progress["steps"]["ma1"]["total"] == 0


def test_no_one_minute_passes_does_not_claim_five_minute_execution():
    result = evaluate(frame=bars().with_columns(pl.lit(10.15).alias("close")))
    assert result.progress["steps"]["ma1"]["passed"] == 0
    assert result.progress["steps"]["ma5"]["state"] == "skipped"
    assert result.progress["steps"]["ma5"]["processed"] is None


def test_empty_market_retains_failed_receipt_with_unknown_counts():
    result = evaluate(frame=pl.DataFrame(), completed_through=TARGET + timedelta(seconds=30))
    assert result.progress["steps"]["breadth"]["state"] == "failed"
    assert result.progress["steps"]["change"]["passed"] is None
    assert result.progress["steps"]["ma1"]["state"] == "skipped"
    assert result.progress["steps"]["breadth"]["reason"]


def test_engine_passes_progress_and_logs_counts_once_until_state_changes(tmp_path, caplog):
    from app.strategy import custom_signals
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, definition(evaluation_delay_minutes=.5))
    prior = pl.DataFrame({"symbol": POOL, "date": [DAY-timedelta(days=1)]*5,
                          "raw_close": [10.]*5})
    context = StrategyDataContext("stock", "1m", DAY, pool_history=prior,
                                  observed_at=TARGET + timedelta(seconds=30))
    with caplog.at_level("INFO"):
        for _ in range(2):
            engine._inject_intraday_signal_columns(
                bars(), context=context, pool_symbols=POOL, diagnostic_symbols=["a", "d"])
    quality = context.intraday_data_quality["csgi_late_session_strength"]
    assert quality["progress"]["steps"]["change"]["passed"] == 1
    records = [r.message for r in caplog.records if "late_session_progress " in r.message]
    assert len(records) == 4
    assert '"processed":' in records[0] and '"scheduled_at":' in records[0]


def test_engine_preserves_failure_progress_when_previous_close_validation_fails(tmp_path, monkeypatch):
    from app.strategy import custom_signals, late_session_strength
    from app.strategy.engine import StrategyDataContext, StrategyEngine

    engine = StrategyEngine([])
    engine._user_data_dir = lambda: tmp_path
    custom_signals.save_one(tmp_path, definition(evaluation_delay_minutes=.5))
    monkeypatch.setattr(late_session_strength, "previous_closes",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("昨收证据不可读")))
    context = StrategyDataContext("stock", "1m", DAY, observed_at=TARGET + timedelta(seconds=30))
    with pytest.raises(ValueError, match="昨收证据不可读"):
        engine._inject_intraday_signal_columns(bars(), context=context, pool_symbols=POOL)
    progress = context.intraday_data_quality["csgi_late_session_strength"]["progress"]
    assert progress["steps"]["breadth"]["state"] == "failed"
    assert "昨收证据不可读" in progress["steps"]["breadth"]["reason"]
