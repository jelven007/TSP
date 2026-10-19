"""Observed-session folds, isolation and account aggregation boundaries."""
from datetime import date
from types import SimpleNamespace

import pytest

from app.backtest.walkforward import (
    WalkForwardConfig,
    WalkForwardService,
    aggregate_oos,
    generate_folds,
)
from app.tickflow.repository import KlineRepository


def _dates(*values):
    return [date.fromisoformat(value) for value in values]


SESSIONS = _dates(
    "2024-02-06", "2024-02-07", "2024-02-08",
    "2024-02-19", "2024-02-20", "2024-02-21", "2024-02-22",
)


def test_session_counts_cross_holiday_without_synthesizing_weekdays():
    folds = generate_folds(
        SESSIONS[0], SESSIONS[-1], 2, 2, 2,
        window_unit="trading_days", purge_days=1, trading_dates=SESSIONS,
    )
    assert len(folds) == 2
    assert (folds[0].train_start, folds[0].train_end) == tuple(SESSIONS[:2])
    assert (folds[0].test_start, folds[0].test_end) == tuple(SESSIONS[3:5])
    assert (folds[1].test_start, folds[1].test_end) == tuple(SESSIONS[5:7])
    assert folds[1].test_start > folds[0].test_end


@pytest.mark.parametrize("sessions", [None, [], SESSIONS[:3]])
def test_missing_calendar_and_insufficient_sessions_fail_closed(sessions):
    with pytest.raises(ValueError, match=r"交易日|不足"):
        generate_folds(
            SESSIONS[0], SESSIONS[-1], 2, 2, 1,
            window_unit="trading_days", trading_dates=sessions,
        )


def test_session_calendar_is_sorted_unique_and_clipped():
    folds = generate_folds(
        SESSIONS[1], SESSIONS[-2], 2, 2, 1,
        window_unit="trading_days", trading_dates=list(reversed(SESSIONS)) + SESSIONS,
    )
    assert folds[0].train_start == SESSIONS[1]
    assert folds[-1].test_end == SESSIONS[-2]


def _record(index, first, last, **stats):
    return {
        "index": index, "test_start": first, "test_end": last,
        "is_score": 1.0, "oos_objective": 0.5,
        "oos_stats": {"total_return": 0.1, **stats},
    }


def test_overlapping_test_windows_keep_diagnostics_without_compounding():
    records = [
        _record(0, "2024-01-01", "2024-01-31"),
        _record(1, "2024-01-31", "2024-02-29"),
    ]
    result = aggregate_oos(records, "sortino")
    assert result["compounded_oos_return"] is None
    assert result["oos_equity_curve"] == []
    assert result["aggregation_unavailable_reason"] == "overlapping_test_windows"
    assert result["avg_oos_objective"] == 0.5
    assert result["consistency"] == 1


@pytest.mark.parametrize("field", ["pending_exit_positions", "unclosed_positions"])
def test_unclosed_positions_are_not_assumed_liquidated_at_boundary(field):
    result = aggregate_oos([_record(0, "2024-01-01", "2024-01-31", **{field: 1})], "sortino")
    assert result["compounded_oos_return"] is None
    assert result["aggregation_unavailable_reason"] == "unclosed_boundary_positions"


def test_adjacent_windows_sorted_by_date_compound_once():
    result = aggregate_oos([
        _record(1, "2024-02-01", "2024-02-29", total_return=-0.1),
        _record(0, "2024-01-01", "2024-01-31"),
    ], "sortino")
    assert result["compounded_oos_return"] == -0.01
    assert [point["fold"] for point in result["oos_equity_curve"]] == [0, 1]


def test_repository_calendar_ignores_empty_malformed_and_other_asset_partitions(tmp_path):
    for directory in (
        "kline_daily_enriched/date=2024-02-08",
        "kline_daily_enriched/date=2024-02-19",
        "kline_daily_enriched/date=bad",
        "kline_daily_enriched/date=2024-02-20",
        "kline_etf_enriched/date=2024-02-21",
    ):
        partition = tmp_path / directory
        partition.mkdir(parents=True)
        if not directory.endswith("02-20"):
            (partition / "part.parquet").touch()
    repo = KlineRepository.__new__(KlineRepository)
    repo.store = SimpleNamespace(data_dir=tmp_path)
    assert repo.list_daily_dates(SESSIONS[0], SESSIONS[-1]) == SESSIONS[2:4]
    assert repo.list_daily_dates(SESSIONS[0], SESSIONS[-1], "etf") == [SESSIONS[5]]


def test_fold_policy_records_observed_calendar_and_legacy_config_default():
    repo = SimpleNamespace(list_daily_dates=lambda *args, **kwargs: SESSIONS)
    svc = SimpleNamespace(engine=SimpleNamespace(repo=repo))
    config = WalkForwardConfig(
        "s", None, SESSIONS[0], SESSIONS[-1], {"p": [1]},
        train_days=2, test_days=2, step_days=2, window_unit="trading_days", purge_days=1,
    )
    folds, policy = WalkForwardService(None, svc, None)._folds(config)
    assert len(folds) == 2
    assert policy["calendar_source"] == "observed_daily_partitions"
    assert policy["calendar_count"] == 7
    assert len(policy["calendar_sha256"]) == 64
    assert WalkForwardConfig("s", None, SESSIONS[0], SESSIONS[-1], {}).window_unit == "calendar_days"
