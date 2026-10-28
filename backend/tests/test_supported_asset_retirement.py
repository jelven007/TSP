"""Retired assets cannot be collected, catalogued or recreated."""
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_scope import supported_symbol
from app.plugins.rustdx.provider import RustdxProvider
from app.services import index_sync
from app.services.index_const import PAGE_INDEX_SYMBOLS
from app.tickflow.repository import DataStore, KlineRepository


def test_runtime_source_contains_no_etf_feature():
    root = Path(__file__).resolve().parents[2]
    matches = []
    etf_token = re.compile(r"(?<![A-Za-z])etf(?![A-Za-z])", re.IGNORECASE)
    for directory in (root / "backend/app", root / "frontend/src"):
        for path in directory.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"} or "__pycache__" in path.parts:
                continue
            text = path.read_text()
            if etf_token.search(text):
                matches.append(str(path.relative_to(root)))
    assert matches == []


@pytest.mark.parametrize("symbol,asset", [
    ("510300.SH", "etf"), ("159915.SZ", "etf"),
    ("600000.SH", "etf"), ("600000.SH", "unknown"),
])
def test_retired_assets_are_rejected(symbol, asset):
    assert not supported_symbol(symbol, asset)


def test_stocks_and_existing_indices_remain_supported():
    assert supported_symbol("600000.SH", "stock")
    assert supported_symbol("000001.SZ", "stock")
    assert all(supported_symbol(symbol, "index") for symbol in PAGE_INDEX_SYMBOLS)
    assert supported_symbol("000688.SH", "index")
    assert supported_symbol("399102.SZ", "index")


def test_index_catalog_sync_preserves_provider_catalog(monkeypatch):
    saved = []
    repo = SimpleNamespace(
        save_index_instruments=saved.append,
        refresh_index_views=lambda: None,
    )
    monkeypatch.setattr(
        index_sync, "_fetch_instruments_by_type",
        lambda *args: pl.DataFrame({
            "symbol": ["000688.SH"],
            "name": ["科创50"],
            "code": ["000688"],
            "asset_type": ["index"],
        }),
    )
    monkeypatch.setattr(index_sync.preferences, "get_daily_data_provider", lambda: "custom")
    assert index_sync.sync_index_instruments(repo) == 8
    assert set(saved[0]["symbol"]) == set(PAGE_INDEX_SYMBOLS) | {"000688.SH"}
    assert set(saved[0]["asset_type"]) == {"index"}


def test_rustdx_realtime_collects_only_stock_catalog(monkeypatch):
    provider = RustdxProvider()
    catalogs = []
    requests = []

    def instruments(asset_type):
        catalogs.append(asset_type)
        return [{"symbol": "600000.SH" if asset_type == "stock" else "510300.SH"}]

    monkeypatch.setattr(provider, "get_instruments", instruments)
    monkeypatch.setattr(
        provider, "_quote_rows",
        lambda symbols, **kwargs: requests.append(symbols) or [],
    )
    provider.get_realtime()
    assert catalogs == ["stock"]
    assert requests == [["600000.SH"]]


def test_rustdx_retired_catalog_does_not_open_connection():
    provider = RustdxProvider(
        client_factory=lambda: pytest.fail("retired catalog must not open a connection"),
    )
    assert provider.get_instruments("etf") == []


def test_new_store_does_not_create_retired_data_directories(tmp_path):
    store = DataStore(tmp_path)
    try:
        assert not [path.name for path in tmp_path.iterdir() if "etf" in path.name]
    finally:
        store.db.close()


def test_write_entrypoints_reject_retired_symbols(tmp_path):
    from app.api import watchlist as watchlist_api
    from app.strategy import paper

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(get_index_symbol_set=lambda: {"000688.SH"}),
    )))
    assert watchlist_api._supported_watchlist_symbol(request, "600000.SH")
    assert watchlist_api._supported_watchlist_symbol(request, "000688.SH")
    assert not watchlist_api._supported_watchlist_symbol(request, "510300.SH")

    paper.create_account(tmp_path, 100_000)
    order, error = paper.create_order(
        tmp_path, "510300.SH", "buy", qty=100, ref_price=4.0,
    )
    assert order is None
    assert error == "模拟交易仅支持股票"


def test_index_catalog_and_history_remain_unfiltered(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    repo.save_index_instruments(pl.DataFrame({
        "symbol": ["000001.SH", "000688.SH", "399102.SZ"],
        "name": ["上证指数", "科创50", "创业板综"], "asset_type": ["index"] * 3,
    }))
    assert repo.get_index_symbol_set() == {"000001.SH", "000688.SH", "399102.SZ"}
    day = date(2026, 10, 9)
    frame = pl.DataFrame({
        "symbol": ["000001.SH", "000688.SH", "399102.SZ"],
        "date": [day] * 3, "close": [3000.0, 1000.0, 4.0],
    })
    repo.append_index_daily(frame)
    out = tmp_path / "kline_index_daily" / f"date={day}" / "part.parquet"
    assert set(pl.read_parquet(out)["symbol"]) == set(frame["symbol"])
    result = repo.read_index_frame("1d", frame["symbol"].to_list(), day, day)
    assert set(result["symbol"]) == set(frame["symbol"])


def test_quote_service_drops_retired_assets_before_writing(monkeypatch):
    from app.services import quote_service
    writes = []
    service = quote_service.QuoteService()
    service._repo = SimpleNamespace(
        get_index_symbol_set=lambda: {"000688.SH", "000001.SH"},
        flush_live_daily=lambda df: writes.append(("stock", df["symbol"].to_list())),
        flush_live_daily_asset=lambda asset, df: writes.append((asset, df["symbol"].to_list())),
        merge_live_daily_asset=lambda asset, df: writes.append((asset, df["symbol"].to_list())),
    )
    monkeypatch.setattr(quote_service, "_persist_last_fetch", lambda ms: None)
    monkeypatch.setattr(service, "_update_volume_delta", lambda *args: None)
    monkeypatch.setattr(service, "_flush_live_enriched", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_broadcast_quote_updated", lambda: None)
    service._process_full_market_records([
        {"symbol": symbol, "last_price": 10.0, "open": 10.0, "high": 10.0,
         "low": 10.0, "volume": 100, "amount": 1000}
        for symbol in ["600000.SH", "000001.SH", "000688.SH", "510300.SH"]
    ], t0=0.0, now_ts=0.0)
    assert writes == [
        ("stock", ["600000.SH"]),
        ("index", ["000001.SH", "000688.SH"]),
    ]
    assert set(service.get_index_quotes()["symbol"]) == {"000001.SH", "000688.SH"}
