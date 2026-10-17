from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import data as data_api
from app.indicators import pipeline
from app.services import financial_sync
from app.tickflow.capabilities import CapabilitySet


def _write_instruments(data_dir, symbols: list[str]) -> None:
    path = data_dir / "instruments" / "instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": symbols}).write_parquet(path)


def test_first_share_sync_fetches_complete_history(tmp_path, monkeypatch):
    _write_instruments(tmp_path, ["600000.SH"])
    calls: list[tuple[list[str], bool]] = []

    def fake_fetch(table, symbols, capset, latest_only=True):
        assert table == "shares"
        calls.append((symbols, latest_only))
        return pl.DataFrame({
            "symbol": ["600000.SH", "600000.SH"],
            "period_end": ["2023-12-31", "2024-06-30"],
            "float_shares": [10.0, 12.0],
        })

    monkeypatch.setattr(financial_sync, "_fetch_table", fake_fetch)

    rows = financial_sync.sync_shares(tmp_path, CapabilitySet())

    assert rows == 2
    assert calls == [(["600000.SH"], False)]
    stored = pl.read_parquet(tmp_path / "financials" / "shares" / "part.parquet")
    assert stored["period_end"].to_list() == ["2023-12-31", "2024-06-30"]


def test_incremental_share_sync_updates_existing_and_backfills_new_symbols(tmp_path, monkeypatch):
    _write_instruments(tmp_path, ["600000.SH", "000001.SZ"])
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "period_end": ["2024-06-30"],
        "float_shares": [10.0],
    }).write_parquet(path)
    calls: list[tuple[list[str], bool]] = []

    def fake_fetch(table, symbols, capset, latest_only=True):
        assert table == "shares"
        calls.append((symbols, latest_only))
        if latest_only:
            return pl.DataFrame({
                "symbol": ["600000.SH"],
                "period_end": ["2024-06-30"],
                "float_shares": [11.0],
            })
        return pl.DataFrame({
            "symbol": ["000001.SZ", "000001.SZ"],
            "period_end": ["2023-12-31", "2024-06-30"],
            "float_shares": [20.0, 21.0],
        })

    monkeypatch.setattr(financial_sync, "_fetch_table", fake_fetch)

    rows = financial_sync.sync_shares(tmp_path, CapabilitySet())

    assert rows == 3
    assert calls == [(["000001.SZ"], False), (["600000.SH"], True)]
    stored = pl.read_parquet(path).sort(["symbol", "period_end"])
    assert stored.filter(pl.col("symbol") == "600000.SH")["float_shares"].to_list() == [11.0]
    assert stored.filter(pl.col("symbol") == "000001.SZ")["float_shares"].to_list() == [20.0, 21.0]


def test_custom_financial_provider_receives_shares_contract(monkeypatch):
    received: list[tuple[str, list[str], bool]] = []

    class Provider:
        def get_financials(self, table, symbols, latest_only=True):
            received.append((table, symbols, latest_only))
            return pl.DataFrame({
                "symbol": symbols,
                "period_end": ["2024-06-30"],
                "float_shares": [10.0],
            })

    from app.data_providers import custom as custom_sources
    from app.services import preferences

    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "custom-test")
    monkeypatch.setattr(custom_sources, "get_provider", lambda _name: Provider())

    result = financial_sync._fetch_table(
        "shares",
        ["600000.SH"],
        CapabilitySet(),
        latest_only=False,
    )

    assert result.height == 1
    assert received == [("shares", ["600000.SH"], False)]


def test_historical_turnover_uses_only_available_share_capital(monkeypatch):
    monkeypatch.setattr(pipeline, "cn_today", lambda: date(2026, 7, 18))
    bars = pl.DataFrame({
        "symbol": ["600000.SH"] * 5,
        "date": [
            date(2024, 3, 31),
            date(2024, 4, 14),
            date(2024, 4, 15),
            date(2024, 6, 30),
            date(2026, 7, 18),
        ],
        "volume": [10_000.0] * 5,
    })
    instruments = pl.DataFrame({
        "symbol": ["600000.SH"],
        "float_shares": [200_000_000.0],
    })
    shares = pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH"],
        "period_end": ["2023-12-31", "2024-06-30"],
        "announce_date": ["2024-04-15", None],
        "float_shares": [100_000_000.0, 50_000_000.0],
    })

    result = pipeline.compute_limit_signals(
        bars,
        instruments,
        needed={"turnover_rate"},
        historical_shares=shares,
    )

    assert result["turnover_rate"].to_list() == pytest.approx([0.5, 0.5, 1.0, 2.0, 0.5])


def test_turnover_without_share_history_keeps_existing_behavior(monkeypatch):
    monkeypatch.setattr(pipeline, "cn_today", lambda: date(2026, 7, 18))
    bars = pl.DataFrame({
        "symbol": ["600000.SH"],
        "date": [date(2024, 4, 15)],
        "volume": [10_000.0],
    })
    instruments = pl.DataFrame({
        "symbol": ["600000.SH"],
        "float_shares": [200_000_000.0],
    })

    result = pipeline.compute_limit_signals(
        bars,
        instruments,
        needed={"turnover_rate"},
    )

    assert result["turnover_rate"][0] == pytest.approx(0.5)


def test_data_status_includes_share_history(tmp_path):
    path = tmp_path / "financials" / "shares" / "part.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({
        "symbol": ["600000.SH", "600000.SH", "000001.SZ"],
        "period_end": ["2023-12-31", "2024-06-30", "2024-06-30"],
    }).write_parquet(path)

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    result = data_api._safe_aggregate_financials(repo)

    assert result is not None
    assert result["rows"] == 3
    assert result["tables"]["shares"] == {"rows": 3, "symbols": 2}


def test_financial_revision_replay_uses_observation_time_and_detects_tampering(tmp_path):
    from datetime import UTC, datetime, timedelta

    from app.services.financial_sync import _write_table, get_financial_revision

    before = datetime.now(UTC) - timedelta(seconds=1)
    old = pl.DataFrame({
        "symbol": ["600000.SH"], "period_end": ["2025-12-31"],
        "announce_date": ["2026-04-01"], "float_shares": [100.0],
    })
    new = old.with_columns(pl.lit(200.0).alias("float_shares"))
    _write_table("shares", old, tmp_path)
    first = datetime.now(UTC)
    _write_table("shares", new, tmp_path)
    second = datetime.now(UTC)
    _write_table("shares", old, tmp_path)
    third = datetime.now(UTC)
    assert get_financial_revision(tmp_path, "shares", before).is_empty()
    assert get_financial_revision(tmp_path, "shares", first)["float_shares"].item() == 100.0
    assert get_financial_revision(tmp_path, "shares", second)["float_shares"].item() == 200.0
    assert get_financial_revision(tmp_path, "shares", third)["float_shares"].item() == 100.0
    revisions = tmp_path / "financials" / "revisions" / "shares"
    assert len(list(revisions.glob("*.parquet"))) == 2
    assert len(list((revisions / "observations").glob("*.json"))) == 3
    for path in revisions.glob("*.parquet"):
        path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        get_financial_revision(tmp_path, "shares", third)


def test_read_financials_as_of_replays_observation_or_reads_live(tmp_path):
    from datetime import UTC, datetime

    from app.services.financial_sync import (
        _write_table,
        latest_financial_revision_meta,
        read_financials_as_of,
    )

    old = pl.DataFrame({
        "symbol": ["600000.SH"], "period_end": ["2025-12-31"],
        "announce_date": ["2026-04-01"], "float_shares": [100.0],
    })
    _write_table("shares", old, tmp_path)
    first = datetime.now(UTC)
    _write_table("shares", old.with_columns(pl.lit(200.0).alias("float_shares")), tmp_path)

    # observed_at=None reads the live current file (latest sync wins).
    assert read_financials_as_of(tmp_path, "shares")["float_shares"].item() == 200.0
    # A past observation time replays the restatement visible back then.
    assert read_financials_as_of(tmp_path, "shares", first)["float_shares"].item() == 100.0

    meta = latest_financial_revision_meta(tmp_path, "shares", first)
    assert meta["content_hash"] and meta["publication_time_verified"] is False
    assert meta["observed_at"] <= first.isoformat()


def test_read_financials_as_of_is_unknown_before_first_observation(tmp_path):
    from datetime import UTC, datetime, timedelta

    from app.services.financial_sync import _write_table, read_financials_as_of

    _write_table("shares", pl.DataFrame({
        "symbol": ["600000.SH"], "period_end": ["2025-12-31"], "float_shares": [100.0],
    }), tmp_path)
    before_any_observation = datetime.now(UTC) - timedelta(days=1)
    # A later live file is not evidence of the values visible at an earlier time.
    frame = read_financials_as_of(tmp_path, "shares", before_any_observation)
    assert frame.is_empty()
