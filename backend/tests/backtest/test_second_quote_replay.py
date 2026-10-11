from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.backtest.second_replay import SecondQuoteReplay, SecondReplayDataError
from app.services.second_quote_archive import build_snapshot_frame


def _round(capture_id: int, collected_at_ms: int, symbols: list[str]) -> pl.DataFrame:
    records = {
        "stock": [
            {
                "symbol": symbol,
                "last_price": 10.0 + index,
                "volume": 100.0,
                "amount": 100_000.0,
                "source_time": f"09:30:0{capture_id}.000",
                "provider_version": "1.12.2@test",
            }
            for index, symbol in enumerate(symbols)
        ],
        "etf": [],
        "index": [],
    }
    return build_snapshot_frame(
        records,
        provider="rustdx",
        capture_id=capture_id,
        collected_at_ms=collected_at_ms,
        trade_date=date(2026, 1, 5),
    )


def _write_part(tmp_path, frames: list[pl.DataFrame], name: str = "part.parquet") -> None:
    out = tmp_path / "quote_second" / "date=2026-01-05" / "hour=09" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    pl.concat(frames, how="vertical_relaxed").write_parquet(out)


def test_iter_snapshots_replays_capture_order_and_filters_symbols(tmp_path) -> None:
    _write_part(
        tmp_path,
        [
            _round(1, 1_767_576_601_000, ["600519.SH", "000001.SZ"]),
            _round(2, 1_767_576_602_000, ["600519.SH", "000001.SZ"]),
            _round(3, 1_767_576_603_000, ["600519.SH", "000001.SZ"]),
        ],
    )
    replay = SecondQuoteReplay(tmp_path)

    snapshots = list(
        replay.iter_snapshots(
            date(2026, 1, 5),
            date(2026, 1, 5),
            symbols=["600519.SH"],
        )
    )

    assert [snapshot.capture_id for snapshot in snapshots] == [1, 2, 3]
    assert [snapshot.collected_at_ms for snapshot in snapshots] == [
        1_767_576_601_000,
        1_767_576_602_000,
        1_767_576_603_000,
    ]
    assert all(snapshot.frame["symbol"].to_list() == ["600519.SH"] for snapshot in snapshots)


def test_validate_reports_gap_and_incomplete_capture(tmp_path) -> None:
    _write_part(
        tmp_path,
        [
            _round(1, 1_767_576_601_000, ["600519.SH", "000001.SZ"]),
            _round(2, 1_767_576_602_000, ["600519.SH", "000001.SZ"]),
            _round(4, 1_767_576_604_000, ["600519.SH"]),
        ],
    )
    replay = SecondQuoteReplay(tmp_path)

    quality = replay.validate(
        date(2026, 1, 5),
        date(2026, 1, 5),
        expected_interval_s=1.0,
        expected_symbols=2,
        min_coverage_ratio=0.95,
        start_ms=1_767_576_601_000,
        end_ms=1_767_576_604_000,
    )

    assert quality["status"] == "warning"
    assert quality["captures"] == 3
    assert quality["rows"] == 5
    assert quality["gap_count"] == 1
    assert quality["max_gap_s"] == 2.0
    assert quality["incomplete_captures"] == 1
    assert quality["source_time_coverage"] == 1.0


def test_validate_marks_contiguous_complete_data_ready(tmp_path) -> None:
    _write_part(
        tmp_path,
        [
            _round(1, 1_767_576_601_000, ["600519.SH", "000001.SZ"]),
            _round(2, 1_767_576_602_000, ["600519.SH", "000001.SZ"]),
        ],
    )
    replay = SecondQuoteReplay(tmp_path)

    quality = replay.validate(
        date(2026, 1, 5),
        date(2026, 1, 5),
        expected_interval_s=1.0,
        expected_symbols=2,
        symbols=["600519.SH", "000001.SZ"],
        start_ms=1_767_576_601_000,
        end_ms=1_767_576_602_000,
    )

    assert quality["status"] == "ready"
    assert quality["gap_count"] == 0
    assert quality["incomplete_captures"] == 0
    assert quality["source_time_coverage"] == 1.0


def test_replay_ignores_unpublished_tmp_files(tmp_path) -> None:
    out = tmp_path / "quote_second" / "date=2026-01-05" / "hour=09"
    out.mkdir(parents=True)
    _round(1, 1_767_576_601_000, ["600519.SH"]).write_parquet(out / "part.parquet.tmp")

    quality = SecondQuoteReplay(tmp_path).validate(
        date(2026, 1, 5),
        date(2026, 1, 5),
    )

    assert quality["status"] == "missing"
    assert quality["files"] == 0


def test_short_samples_never_claim_whole_day_ready(tmp_path):
    _write_part(tmp_path, [_round(1, 1_767_576_601_000, ["600519.SH"])])
    result = SecondQuoteReplay(tmp_path).validate(date(2026, 1, 5), date(2026, 1, 5))
    assert result["status"] == "warning"
    assert result["scope"] == "observed_samples"
    assert "window_or_expected_universe_unspecified" in result["issues"]


def test_lunch_gap_is_excluded_but_session_edges_are_checked(tmp_path):
    from app.backtest.second_replay import session_windows

    day = date(2026, 1, 5)
    morning_end = session_windows(day)[0][1]
    afternoon_start = session_windows(day)[1][0]
    frames = []
    for capture, stamp in enumerate((morning_end - 1000, morning_end, afternoon_start, afternoon_start + 1000), 1):
        frames.append(_round(capture, stamp, ["600519.SH"]).with_columns(pl.lit(stamp).alias("source_ts_ms")))
    _write_part(tmp_path, frames)
    result = SecondQuoteReplay(tmp_path).validate(
        day, day, symbols=["600519.SH"], start_ms=morning_end - 1000, end_ms=afternoon_start + 1000,
    )
    assert result["status"] == "ready"
    assert result["gap_count"] == 0


def test_frozen_replay_excludes_new_files_and_detects_mutation(tmp_path):
    day = date(2026, 1, 5)
    _write_part(tmp_path, [_round(1, 1_767_576_601_000, ["600519.SH"])])
    replay = SecondQuoteReplay(tmp_path)
    manifest = replay.manifest(day, day)
    _write_part(tmp_path, [_round(2, 1_767_576_602_000, ["600519.SH"])], "new.parquet")
    assert [item.capture_id for item in replay.iter_snapshots(day, day)] == [1]
    assert replay.manifest(day, day) == manifest
    _write_part(tmp_path, [_round(3, 1_767_576_603_000, ["600519.SH"])])
    with pytest.raises(SecondReplayDataError, match="changed"):
        list(replay.iter_snapshots(day, day))


def test_invalid_schema_and_overlapping_shards_fail_closed(tmp_path):
    day = date(2026, 1, 5)
    frame = _round(1, 1_767_576_601_000, ["600519.SH"])
    _write_part(tmp_path, [frame.with_columns(pl.lit(99).alias("schema_version"))])
    with pytest.raises(SecondReplayDataError, match="schema"):
        list(SecondQuoteReplay(tmp_path).iter_snapshots(day, day))
    _write_part(tmp_path, [frame], "overlap.parquet")
    with pytest.raises(SecondReplayDataError, match="overlapping"):
        list(SecondQuoteReplay(tmp_path).iter_snapshots(day, day))


def test_missing_entire_trading_day_is_reported(tmp_path):
    from app.backtest.second_replay import session_windows

    start, end = date(2026, 1, 5), date(2026, 1, 6)
    _write_part(tmp_path, [_round(1, 1_767_576_601_000, ["600519.SH"])])
    result = SecondQuoteReplay(tmp_path).validate(
        start, end, symbols=["600519.SH"], start_ms=1_767_576_601_000,
        end_ms=session_windows(end)[1][1], trading_dates=[start, end],
    )
    assert result["status"] == "warning"
    assert result["missing_dates"] == ["2026-01-06"]


def test_bad_source_time_and_cumulative_reset_are_not_ready(tmp_path):
    first = _round(1, 1_767_576_601_000, ["600519.SH"])
    second = _round(2, 1_767_576_602_000, ["600519.SH"]).with_columns(
        pl.lit(1_767_576_600_000).alias("source_ts_ms"), pl.lit(50.0).alias("volume"),
    )
    _write_part(tmp_path, [first, second])
    result = SecondQuoteReplay(tmp_path).validate(
        date(2026, 1, 5), date(2026, 1, 5), symbols=["600519.SH"],
        start_ms=1_767_576_601_000, end_ms=1_767_576_602_000,
    )
    assert result["source_regressions"] == 1
    assert result["cumulative_resets"] == 1
    assert result["status"] == "warning"


def test_duplicate_unselected_security_is_still_rejected(tmp_path):
    _write_part(tmp_path, [_round(1, 1_767_576_601_000, ["600519.SH", "000001.SZ", "000001.SZ"])])
    with pytest.raises(SecondReplayDataError, match="duplicate security"):
        list(SecondQuoteReplay(tmp_path).iter_snapshots(
            date(2026, 1, 5), date(2026, 1, 5), symbols=["600519.SH"],
        ))


def test_capture_id_regression_across_shards_is_rejected(tmp_path):
    _write_part(tmp_path, [_round(2, 1_767_576_601_000, ["600519.SH"])])
    _write_part(tmp_path, [_round(1, 1_767_576_602_000, ["600519.SH"])], "later.parquet")
    with pytest.raises(SecondReplayDataError, match="regressed across shards"):
        list(SecondQuoteReplay(tmp_path).iter_snapshots(date(2026, 1, 5), date(2026, 1, 5)))


def test_unknown_version_cannot_pass_research_gate(tmp_path):
    _write_part(tmp_path, [
        _round(i, 1_767_576_600_000 + i * 1000, ["600519.SH"])
        .with_columns(pl.lit("unknown").alias("provider_version"))
        for i in (1, 2)
    ])
    result = SecondQuoteReplay(tmp_path).validate(
        date(2026, 1, 5), date(2026, 1, 5), symbols=["600519.SH"],
        start_ms=1_767_576_601_000, end_ms=1_767_576_602_000,
    )
    assert "unknown_provider_version" in result["issues"]
    assert result["status"] == "warning"
