"""rustdx provider contracts without requiring a live trading session."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.data_providers import custom as custom_sources
from app.plugins.rustdx import client as rustdx_client
from app.plugins.rustdx.client import RustdxClient, RustdxError
from app.plugins.rustdx.provider import RustdxProvider
from app.services import preferences

ROOT = Path(__file__).resolve().parents[2]


class _Native:
    max_connections = 35

    def __init__(self):
        self.quote_calls = []
        self.bar_calls = []

    def quotes_json(self, securities):
        self.quote_calls.append(securities)
        return json.dumps(
            [
                {
                    "market": market,
                    "code": code,
                    "price": 10.5,
                    "last_close": 10,
                    "vol": 123,
                    "amount": 129_150,
                }
                for market, code in securities
            ]
        )

    def bars_json(self, market, code, category, start, count, index):
        self.bar_calls.append((market, code, category, start, count, index))
        return json.dumps(
            [
                {
                    "dt": {"year": 2026, "month": 10, "day": 9, "hour": 15, "minute": 0},
                    "code": code,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 123,
                    "amount": 129_150,
                }
            ]
        )

    def stats_json(self):
        return json.dumps({"max_connections": 35, "quote_batch_size": 60})


@pytest.fixture
def native(monkeypatch):
    value = _Native()
    monkeypatch.setattr(rustdx_client, "_native_client", lambda: value)
    return value


def test_client_preserves_explicit_exchange_and_native_pool(native):
    client = RustdxClient()

    rows = client.quotes(["000001.SH", "000001.SZ"])

    assert native.quote_calls == [[(1, "000001"), (0, "000001")]]
    assert [(row["market"], row["code"]) for row in rows] == [
        (1, "000001"),
        (0, "000001"),
    ]
    assert client.max_connections == 35
    assert client.stats()["quote_batch_size"] == 60


def test_client_normalizes_rust_datetime_and_index_category(native):
    client = RustdxClient()

    rows = client.bars("000001.SH", frequency=9)

    assert rows[0]["datetime"] == "2026-10-09 15:00"
    assert native.bar_calls == [(1, "000001", 4, 0, 800, True)]


def test_client_normalizes_tdx_zero_sentinel(native):
    native.bars_json = lambda *_args: json.dumps(
        [
            {
                "dt": {"year": 2026, "month": 10, "day": 9, "hour": 14, "minute": 58},
                "code": "600519",
                "vol": 2.0**-127,
                "amount": 2.0**-127,
            }
        ]
    )

    row = RustdxClient().bars("600519.SH", frequency=8)[0]

    assert row["vol"] == 0.0
    assert row["amount"] == 0.0


def test_client_corrects_new_sh_etf_price_coefficient(native):
    native.quotes_json = lambda *_args: json.dumps(
        [
            {
                "market": 1,
                "code": "588200",
                "price": 15.32,
                "last_close": 15.30,
                "bid1": 15.31,
                "ask1": 15.32,
                "vol": 300,
            }
        ]
    )

    row = RustdxClient().quotes(["588200.SH"])[0]

    assert row["price"] == pytest.approx(1.532)
    assert row["last_close"] == pytest.approx(1.53)
    assert row["bid1"] == pytest.approx(1.531)
    assert row["vol"] == 300


def test_rustdx_depth_preserves_five_level_quantities(native):
    native.quotes_json = lambda *_args: json.dumps(
        [
            {
                "market": 1,
                "code": "600519",
                "price": 10,
                **{f"bid{level}_vol": level * 10 for level in range(1, 6)},
                **{f"ask{level}_vol": level * 20 for level in range(1, 6)},
            }
        ]
    )
    depth = RustdxProvider().get_depth_batch(["600519.SH"])["600519.SH"]
    assert depth["bid_volumes"] == [10, 20, 30, 40, 50]
    assert depth["ask_volumes"] == [20, 40, 60, 80, 100]


def test_client_rejects_unsupported_exchange(native):
    with pytest.raises(RustdxError, match="无效交易所"):
        RustdxClient().quotes(["920001.BJ"])


def test_provider_caps_full_market_workers_at_35(monkeypatch):
    monkeypatch.setenv("RUSTDX_WORKERS", "99")
    monkeypatch.setenv("RUSTDX_QUOTE_CONNECTIONS", "99")

    assert RustdxProvider._workers(5_223) == 35
    assert rustdx_client._connection_count() == 35


def test_native_bridge_clamps_direct_constructor_to_35():
    native_module = pytest.importorskip("tsp_rustdx_native")
    assert native_module.RUSTDX_VERSION == "1.12.2"
    assert (
        native_module.RUSTDX_SOURCE_REV
        == "9dfc024c9662fa5091099c1f17610569ee2eca8b"
    )
    client = native_module.RustdxClient(100)
    try:
        assert client.max_connections == 35
        assert json.loads(client.stats_json())["quote_batch_size"] == 60
    finally:
        client.close()


def test_missing_native_bridge_is_isolated(monkeypatch):
    real_import = rustdx_client.importlib.import_module

    def missing(name):
        if name == "tsp_rustdx_native":
            raise ImportError("not installed")
        return real_import(name)

    monkeypatch.setattr(rustdx_client.importlib, "import_module", missing)

    available, reason = rustdx_client.availability()

    assert available is False
    assert "缺少 rustdx 原生桥接" in reason


def test_native_bridge_dependency_mismatch_is_rejected(monkeypatch):
    class StaleNative:
        MAX_CONNECTIONS = 35
        __version__ = "0.1.0"
        RUSTDX_VERSION = "1.11.0"
        RUSTDX_SOURCE_REV = "stale"

    monkeypatch.setattr(
        rustdx_client.importlib,
        "import_module",
        lambda _name: StaleNative,
    )

    available, reason = rustdx_client.availability()

    assert available is False
    assert "依赖版本与项目锁定版本不一致" in reason


def test_old_bridge_with_current_sdk_requires_rebuild(monkeypatch):
    from types import SimpleNamespace

    old = SimpleNamespace(
        MAX_CONNECTIONS=35, __version__="0.1.0",
        RUSTDX_VERSION="1.12.2", RUSTDX_SOURCE_REV=rustdx_client._RUSTDX_SOURCE_REV,
    )
    monkeypatch.setattr(rustdx_client.importlib, "import_module", lambda name: old)
    available, reason = rustdx_client.availability()
    assert available is False
    assert "重新构建" in reason


def test_provider_uses_one_native_full_market_call():
    calls = []

    class Client:
        def quotes(self, symbols):
            calls.append(symbols)
            return [{"market": 1, "code": "600519", "price": 10}]

        @staticmethod
        def close_shared():
            pass

    provider = RustdxProvider(client_factory=Client)

    assert provider._persistent_quotes(
        ["600519.SH", "000001.SZ"],
        operation="实时行情",
    ) == [{"market": 1, "code": "600519", "price": 10}]
    assert calls == [["600519.SH", "000001.SZ"]]


def test_realtime_quote_preserves_rustdx_source_time() -> None:
    class Client:
        def quotes(self, _symbols):
            return [{
                "market": 1,
                "code": "600519",
                "price": 10.5,
                "last_close": 10.0,
                "vol": 123,
                "amount": 129_150,
                "servertime": "09:30:01.250",
            }]

        @staticmethod
        def close_shared():
            pass

    rows = RustdxProvider(client_factory=Client)._quote_rows(
        ["600519.SH"],
        include_names=False,
    )

    assert rows[0]["source_time"] == "09:30:01.250"


def test_rustdx_instrument_directory_skips_per_symbol_finance():
    class CatalogClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stocks(self, market):
            return (
                [{"code": "000001", "name": "平安银行"}]
                if market == 0
                else [{"code": "600519", "name": "贵州茅台"}]
            )

        def finance(self, _symbol):
            raise AssertionError("rustdx directory must not fetch finance per symbol")

        @staticmethod
        def close_shared():
            pass

    provider = RustdxProvider(client_factory=CatalogClient)

    rows = provider.get_instruments("stock")

    assert [row["symbol"] for row in rows] == ["000001.SZ", "600519.SH"]
    assert all(row["ext"]["float_shares"] is None for row in rows)


def test_rustdx_explicit_metadata_normalizes_ipo_and_shares_and_caches():
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def finance(self, symbol):
            calls.append(symbol)
            return [{"ipo_date": 19910403, "zongguben": 1e9, "liutongguben": 8e8}]

    provider = RustdxProvider(client_factory=Client)
    symbols = ["000001.SZ", "000001.SZ", "000001.SH", "510300.SH"]
    expected = [{
        "symbol": "000001.SZ", "listing_date": "1991-04-03",
        "total_shares": 1e9, "float_shares": 8e8,
    }]
    assert provider.get_instrument_metadata(symbols) == expected
    assert provider.get_instrument_metadata(symbols) == expected
    assert calls == ["000001.SZ"]


@pytest.mark.parametrize(
    ("bars", "finance", "expected"),
    [
        ([], [{"ipo_date": 0, "zongguben": 0, "liutongguben": 0}], False),
        ([{"close": 10}], [], True),
        ([], [{"ipo_date": 20260101, "zongguben": 1000, "liutongguben": 500}], True),
        ([], [], True),
        ([], [{"ipo_date": 0}], True),
    ],
)
def test_catalog_excludes_only_confirmed_pending_listings(bars, finance, expected):
    calls = []

    class CatalogClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stocks(self, market):
            return [
                {"code": "001381", "name": "待上市股票", "pre_close": 2.0**-127},
                {"code": "000001", "name": "平安银行", "pre_close": 10},
            ] if market == 0 else [{"code": "600519", "name": "贵州茅台"}]

        def bars(self, symbol, **kwargs):
            calls.append(("bars", symbol, kwargs))
            return bars

        def finance(self, symbol):
            calls.append(("finance", symbol))
            return finance

    rows = RustdxProvider(client_factory=CatalogClient).get_instruments("stock")

    symbols = {row["symbol"] for row in rows}
    assert ("001381.SZ" in symbols) is expected
    assert {"000001.SZ", "600519.SH"} <= symbols
    assert calls[0] == ("bars", "001381.SZ", {"frequency": 9, "offset": 1})
    assert all(call[1] == "001381.SZ" for call in calls)


def test_pending_listing_verification_failure_preserves_previous_catalog():
    class CatalogClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stocks(self, market):
            return [
                {"code": "001381", "name": "待上市股票", "pre_close": 2.0**-127},
            ] if market == 0 else [{"code": "600519", "name": "贵州茅台"}]

        def bars(self, *_args, **_kwargs):
            raise RustdxError("verification unavailable")

    assert RustdxProvider(client_factory=CatalogClient).get_instruments("stock") == []


def test_rustdx_daily_rows_follow_shared_unit_contract():
    frame = RustdxProvider._daily_frame(
        [
            {
                "datetime": "2026-10-09 15:00",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 123,
                "amount": 129_150,
            }
        ],
        "600519.SH",
        datetime(2026, 10, 9),
        datetime(2026, 10, 9, 15),
    )

    assert frame["volume"].to_list() == [123.0]


def test_incremental_minutes_request_only_latest_real_bars(native, monkeypatch):
    from app.plugins.rustdx import provider as provider_module

    monkeypatch.setattr(provider_module, "cn_now", lambda: datetime(2026, 10, 9, 15, 1))
    frame = RustdxProvider().get_intraday_latest(["600519.SH", "000001.SZ"], count=3)
    assert len(native.bar_calls) == 2
    assert all(call[2:5] == (8, 0, 3) for call in native.bar_calls)
    assert frame["volume"].to_list() == [1.23, 1.23]
    assert frame["datetime"].to_list() == [datetime(2026, 10, 9, 15)] * 2


def test_incremental_minutes_defaults_to_stock_universe(native, monkeypatch):
    from app.plugins.rustdx import provider as provider_module

    monkeypatch.setattr(provider_module, "cn_now", lambda: datetime(2026, 10, 9, 15, 1))
    provider = RustdxProvider()
    monkeypatch.setattr(
        provider,
        "get_instruments",
        lambda asset_type: [
            {"symbol": "600519.SH"},
            {"symbol": "000001.SZ"},
        ] if asset_type == "stock" else [],
    )

    frame = provider.get_intraday_latest(count=3)

    assert len(native.bar_calls) == 2
    assert frame["symbol"].to_list() == ["000001.SZ", "600519.SH"]


def test_counted_increment_uses_actual_attempts_and_deduplicates_symbols(native, monkeypatch):
    from app.plugins.rustdx import provider as provider_module
    from app.services.kline_sync import fetch_intraday_custom_latest

    monkeypatch.setattr(provider_module, "cn_now", lambda: datetime(2026, 10, 9, 15, 1))
    bars = native.bars_json

    def counted(*args, **kwargs):
        assert kwargs == {"with_request_count": True}
        return json.dumps({"rows": json.loads(bars(*args)), "requests": 2, "error": None})

    native.bars_json = counted
    frame, requests = fetch_intraday_custom_latest(
        RustdxProvider(), "rustdx", symbols=["600519.SH", "600519.SH", "000001.SZ"],
    )
    assert requests == 4
    assert len(native.bar_calls) == 2
    assert frame.height == 2


def test_counted_full_minute_repair_reports_zero_requests_for_empty_universe(native):
    from app.services.kline_sync import fetch_intraday_custom_batch

    frame, requests = fetch_intraday_custom_batch(RustdxProvider(), "rustdx", [])
    assert frame.is_empty()
    assert requests == 0
    assert native.bar_calls == []


@pytest.mark.parametrize("fetcher", ["fetch_intraday_custom_latest", "fetch_intraday_custom_batch"])
def test_counted_minutes_preserve_afternoon_opening_label(native, monkeypatch, fetcher):
    from app.plugins.rustdx import provider as provider_module
    from app.services import kline_sync

    monkeypatch.setattr(provider_module, "cn_now", lambda: datetime(2026, 10, 14, 13, 2))

    def counted(market, code, category, start, count, index, **kwargs):
        native.bar_calls.append((market, code, category, start, count, index))
        assert kwargs == {"with_request_count": True}
        return json.dumps({
            "rows": [
                {
                    "dt": {"year": 2026, "month": 10, "day": 14, "hour": 13, "minute": minute},
                    "code": code, "close": 10.0, "vol": 100, "amount": 1000,
                }
                for minute in (0, 1, 2)
            ],
            "requests": 2,
            "error": None,
        })

    native.bars_json = counted
    frame, requests = getattr(kline_sync, fetcher)(
        RustdxProvider(), "rustdx", symbols=["600519.SH", "000001.SZ"],
    )
    assert requests == 4
    assert frame.height == 6
    assert frame["datetime"].to_list() == [
        datetime(2026, 10, 14, 13, minute) for minute in (0, 1, 2)
    ] * 2
    assert frame["volume"].to_list() == [1.0] * 6


@pytest.mark.parametrize("requests", [0, 3])
def test_counted_bars_raise_protocol_failure_even_with_returned_rows(native, requests):
    rows = json.loads(native.bars_json(1, "600519", 8, 0, 3, False))
    native.bars_json = lambda *args, **kwargs: json.dumps({
        "rows": rows, "requests": requests, "error": "injected protocol failure",
    })

    with pytest.raises(RustdxError, match="injected protocol failure") as caught:
        RustdxClient().bars_with_count("600519.SH", frequency=8, offset=3)

    assert caught.value.requests == requests


def test_counted_bars_successfully_empty_is_not_protocol_failure(native):
    native.bars_json = lambda *args, **kwargs: json.dumps({
        "rows": [], "requests": 1, "error": None,
    })
    assert RustdxClient().bars_with_count("600519.SH", frequency=8, offset=3) == ([], 1)


@pytest.mark.parametrize(
    "method", ["get_intraday_latest_with_count", "get_intraday_batch_with_count"],
)
@pytest.mark.parametrize("workers", [1, 2])
def test_counted_minutes_reject_failed_window_and_preserve_all_request_counts(
    native, monkeypatch, method, workers,
):
    from app.plugins.rustdx import provider as provider_module

    monkeypatch.setenv("RUSTDX_WORKERS", str(workers))
    monkeypatch.setattr(provider_module, "cn_now", lambda: datetime(2026, 10, 9, 15, 1))
    bars = native.bars_json

    def counted(market, code, *args, **kwargs):
        rows = json.loads(bars(market, code, *args))
        return json.dumps({
            "rows": [] if code == "000001" else rows,
            "requests": 3 if code == "000001" else 1,
            "error": "injected protocol failure" if code == "000001" else None,
        })

    native.bars_json = counted
    symbols = ["600519.SH", "000001.SZ", "300750.SZ", "600000.SH", "000002.SZ"]
    with pytest.raises(RustdxError, match=r"000001\.SZ") as caught:
        getattr(RustdxProvider(), method)(symbols)

    assert caught.value.requests == workers * 2 + 2
    assert {call[1] for call in native.bar_calls} == {
        symbol.split(".")[0] for symbol in symbols[:workers * 2]
    }


def test_plugin_covers_all_core_datasets_and_is_default_provider():
    plugins = {plugin["name"]: plugin for plugin in custom_sources.list_plugins()}

    assert set(plugins["rustdx"]["datasets"]) == {
        "realtime",
        "daily",
        "adj_factor",
        "corporate_actions",
        "minute",
        "depth5",
        "financial",
        "full_minute",
    }
    assert preferences._DEFAULT_DATA_PROVIDER == "rustdx"


def test_registry_defaults_to_rustdx_and_retains_tickflow_and_plugins(monkeypatch):
    from app.data_providers.registry import get_provider
    from app.data_providers.tickflow_provider import TickFlowProvider

    assert isinstance(get_provider(), RustdxProvider)
    assert isinstance(get_provider("tickflow"), TickFlowProvider)
    fallback = object()
    calls = []
    monkeypatch.setattr(
        custom_sources, "get_provider", lambda name: calls.append(name) or fallback,
    )
    assert get_provider("fuyao") is fallback
    assert calls == ["fuyao"]


def test_native_bridge_declares_bounded_parallel_quote_batches():
    source = (ROOT / "backend" / "native" / "rustdx_native" / "src" / "lib.rs").read_text(
        encoding="utf-8"
    )

    assert "const MAX_CONNECTIONS: usize = 35;" in source
    assert "const QUOTE_BATCH_SIZE: usize = 60;" in source
    assert ".chunks(QUOTE_BATCH_SIZE)" in source
    assert "max_connections.min(batches.len()).max(1)" in source
    assert "index as u64 * 20" in source
