from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers import custom as custom_sources
from app.services import instrument_sync


def test_catalog_refresh_preserves_valid_metadata_without_refreshing_its_age(
    tmp_path, monkeypatch,
):
    state = {
        "day": date(2026, 7, 1),
        "rows": [{
            "symbol": "600001.SH", "name": "示例",
            "listing_date": "2020-01-01", "total_shares": 1000.0,
            "float_shares": 800.0, "tick_size": 0.01, "limit_up": 11.0,
        }, {"symbol": "600002.SH", "name": "旧标的", "total_shares": 900.0}],
    }
    monkeypatch.setattr(instrument_sync, "_fetch_instruments_via_provider", lambda: state["rows"])
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: state["day"])
    monkeypatch.setattr("app.services.preferences.get_daily_data_provider", lambda: "rustdx")
    instrument_sync.sync_instruments(tmp_path)
    state["day"] = date(2026, 7, 2)
    state["rows"] = [{
        "symbol": "600001.SH", "name": "*ST示例",
        "listing_date": "", "total_shares": None, "float_shares": 850.0,
        "tick_size": 0.0,
    }, {"symbol": "600003.SH", "name": "新标的"}]

    assert instrument_sync.sync_instruments(tmp_path) == 2
    current = pl.read_parquet(tmp_path / "instruments/instruments.parquet")
    row = current.filter(pl.col("symbol") == "600001.SH").row(0, named=True)
    assert row["name"] == "*ST示例"
    assert row["listing_date"] == "2020-01-01"
    assert row["total_shares"] == 1000.0
    assert row["tick_size"] == 0.01
    assert row["float_shares"] == 850.0
    assert row["limit_up"] is None
    assert row["total_shares_as_of"] == date(2026, 7, 1)
    assert row["total_shares_source"] == "rustdx"
    assert row["float_shares_as_of"] == date(2026, 7, 2)
    assert current["symbol"].to_list() == ["600001.SH", "600003.SH"]
    assert current.filter(pl.col("symbol") == "600003.SH")["total_shares"][0] is None


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
def test_invalid_provider_share_value_cannot_erase_valid_legacy_metadata(
    tmp_path, monkeypatch, invalid,
):
    target = tmp_path / "instruments/instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame({
        "symbol": ["600001.SH"], "name": ["示例"],
        "listing_date": [date(2020, 1, 1)],
        "total_shares": [1000.0], "as_of": [date(2026, 7, 1)],
    }).write_parquet(target)
    monkeypatch.setattr(instrument_sync, "_fetch_instruments_via_provider", lambda: [{
        "symbol": "600001.SH", "name": "示例", "total_shares": invalid,
    }])
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: date(2026, 7, 2))
    monkeypatch.setattr("app.services.preferences.get_daily_data_provider", lambda: "rustdx")
    instrument_sync.sync_instruments(tmp_path)
    row = pl.read_parquet(target).row(0, named=True)
    assert row["listing_date"] == "2020-01-01"
    assert row["total_shares"] == 1000.0
    assert row["total_shares_source"] is None
    assert row["total_shares_as_of"] == date(2026, 7, 1)


def test_sync_enriches_fresh_catalog_through_optional_provider_metadata(tmp_path, monkeypatch):
    calls = []

    def metadata(symbols):
        calls.append(symbols)
        return [{
            "symbol": "600001.SH", "listing_date": "2000-01-01",
            "total_shares": 1e9, "float_shares": 8e8,
        }, {"symbol": "600999.SH", "total_shares": 99.0}]

    provider = SimpleNamespace(
        get_instruments=lambda asset: [{"symbol": "600001.SH", "name": "示例"}],
        get_instrument_metadata=metadata,
    )
    monkeypatch.setattr("app.services.preferences.get_daily_data_provider", lambda: "rustdx")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)

    assert instrument_sync.sync_instruments(tmp_path) == 1
    row = pl.read_parquet(tmp_path / "instruments/instruments.parquet").row(0, named=True)
    assert calls == [["600001.SH"]]
    assert row["listing_date"] == "2000-01-01"
    assert row["total_shares"] == 1e9
    assert row["float_shares"] == 8e8
    assert row["total_shares_source"] == "rustdx"


def test_optional_metadata_failure_keeps_valid_catalog_fields(monkeypatch):
    def unavailable(_symbols):
        raise RuntimeError("metadata unavailable")

    provider = SimpleNamespace(
        get_instruments=lambda asset: [{
            "symbol": "600001.SH", "name": "示例", "ext": {"total_shares": 1e9},
        }],
        get_instrument_metadata=unavailable,
    )
    monkeypatch.setattr("app.services.preferences.get_daily_data_provider", lambda: "rustdx")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    rows = instrument_sync._fetch_instruments_via_provider()
    assert len(rows) == 1
    assert rows[0]["total_shares"] == 1e9
