from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers.instrument_status import current_a_share_board
from app.jobs import daily_pipeline
from app.services import instrument_sync


@pytest.mark.parametrize(
    ("symbol", "board"),
    [
        ("600000.SH", "沪主板"),
        ("605999.SH", "沪主板"),
        ("000001.SZ", "深主板"),
        ("003999.SZ", "深主板"),
        ("300001.SZ", "创业板"),
        ("301999.SZ", "创业板"),
        ("688001.SH", "科创板"),
        ("689009.SH", "科创板"),
    ],
)
def test_current_a_share_board_boundaries(symbol, board):
    assert current_a_share_board(symbol) == board


@pytest.mark.parametrize(
    "symbol",
    ["000001.SH", "510300.SH", "200001.SZ", "430047.BJ", "920001.BJ"],
)
def test_current_a_share_board_rejects_non_four_board_symbols(symbol):
    assert current_a_share_board(symbol) is None


def test_load_current_stock_pool_supports_legacy_snapshot(tmp_path):
    target = tmp_path / "instruments" / "instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame(
        {
            "symbol": [
                "600001.SH",
                "000001.SZ",
                "300001.SZ",
                "688001.SH",
                "510300.SH",
            ],
            "name": ["沪", "深", "创", "科", "ETF"],
            "as_of": [date(2026, 10, 15)] * 5,
        }
    ).write_parquet(target)

    snapshot = instrument_sync.load_current_stock_pool(tmp_path)

    assert snapshot.symbols == (
        "000001.SZ",
        "300001.SZ",
        "600001.SH",
        "688001.SH",
    )
    assert snapshot.as_of == date(2026, 10, 15)
    assert snapshot.source == "legacy"
    assert set(snapshot.boards) == {"沪主板", "深主板", "创业板", "科创板"}


def test_load_current_stock_pool_fails_without_snapshot(tmp_path):
    with pytest.raises(
        instrument_sync.InstrumentSnapshotUnavailableError,
        match="没有可用的股票池快照",
    ):
        instrument_sync.load_current_stock_pool(tmp_path)


def test_complete_stock_pool_requires_all_four_boards(tmp_path):
    target = tmp_path / "instruments" / "instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ", "300001.SZ"],
            "name": ["沪", "深", "创"],
        }
    ).write_parquet(target)

    with pytest.raises(
        instrument_sync.InstrumentSnapshotUnavailableError,
        match="缺少: 科创板",
    ):
        instrument_sync.load_current_stock_pool(tmp_path, require_complete=True)


def test_instrument_job_retains_last_valid_snapshot(tmp_path, monkeypatch):
    target = tmp_path / "instruments" / "instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ", "300001.SZ", "688001.SH"],
            "name": ["沪", "深", "创", "科"],
            "as_of": [date(2026, 10, 15)] * 4,
            "snapshot_source": ["rustdx"] * 4,
        }
    ).write_parquet(target)
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda repo: None)
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda table=None: None)
    monkeypatch.setattr(
        "app.services.data_release.publish_data_release",
        lambda *args, **kwargs: {"release_id": "release-test"},
    )
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    result = daily_pipeline.run_instruments_sync(repo)

    assert result["instruments_rows"] == 4
    assert result["instruments_refreshed"] is False
    assert result["instruments_as_of"] == "2026-10-15"
    assert result["instruments_source"] == "rustdx"


def test_instrument_job_fails_on_fresh_install_without_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(instrument_sync, "sync_instruments", lambda data_dir: 0)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))

    with pytest.raises(
        instrument_sync.InstrumentSnapshotUnavailableError,
        match="没有可用的股票池快照",
    ):
        daily_pipeline.run_instruments_sync(repo)


def test_partial_catalog_does_not_replace_complete_snapshot(tmp_path, monkeypatch):
    target = tmp_path / "instruments" / "instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame(
        {
            "symbol": ["600001.SH", "000001.SZ", "300001.SZ", "688001.SH"],
            "name": ["沪", "深", "创", "科"],
        }
    ).write_parquet(target)
    before = target.read_bytes()
    monkeypatch.setattr(
        instrument_sync,
        "_fetch_instruments_via_provider",
        lambda: [{"symbol": "600002.SH", "name": "仅沪市"}],
    )
    monkeypatch.setattr(
        "app.services.preferences.get_daily_data_provider",
        lambda: "rustdx",
    )

    assert instrument_sync.sync_instruments(tmp_path) == 0
    assert target.read_bytes() == before
