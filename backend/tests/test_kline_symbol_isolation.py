"""A failed security must not discard healthy securities or hide remaining gaps."""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.plugins.rustdx.client import RustdxError
from app.plugins.rustdx.provider import RustdxProvider
from app.services import kline_gaps, kline_periods
from app.tickflow.repository import DataStore, KlineRepository


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


@pytest.mark.parametrize("dataset", ["daily", "minute"])
def test_client_initialization_failure_is_not_a_skipped_security(dataset):
    class Unavailable(Client):
        def __enter__(self):
            raise RustdxError("服务连接不可用")
    provider = RustdxProvider(client_factory=Unavailable)
    with pytest.raises(RustdxError, match="服务连接不可用"):
        getattr(provider, f"get_{dataset}")(["600000.SH"], None, None)


@pytest.mark.parametrize("dataset", ["daily", "minute"])
@pytest.mark.parametrize("workers", [1, 2])
def test_three_failed_securities_do_not_skip_later_healthy_security(monkeypatch, caplog, dataset, workers):
    provider = RustdxProvider(client_factory=Client)
    monkeypatch.setattr(provider, "_workers", lambda _: workers)
    calls = []
    symbols = ["301110.SZ", "301111.SZ", "301112.SZ", "600000.SH"]

    def fetch(_client, symbol, *_):
        calls.append(symbol)
        if symbol != symbols[-1]:
            raise RustdxError("源未返回完整历史")
        return [{"datetime": datetime(2026, 10, 23, 15), "open": 10., "high": 11.,
                 "low": 9., "close": 10., "volume": 100., "amount": 1000.}]

    monkeypatch.setattr(provider, "_bar_records", fetch)
    frame = getattr(provider, f"get_{dataset}")(symbols, datetime(2026, 10, 23), datetime(2026, 10, 23, 15))
    assert frame["symbol"].to_list() == [symbols[-1]]
    assert set(calls) == set(symbols)
    assert "301110.SZ" in caplog.text


@pytest.mark.parametrize("mode", ["missing", "full"])
def test_missing_stock_is_skipped_and_its_exact_gap_is_durable(tmp_path, monkeypatch, mode):
    repo = KlineRepository(DataStore(tmp_path))
    symbols = ["301110.SZ", "600000.SH"]
    day = date(2026, 10, 23)
    monkeypatch.setattr("app.services.instrument_sync.load_current_stock_pool", lambda *_a, **_k: SimpleNamespace(symbols=symbols))
    monkeypatch.setattr(repo, "get_instruments", lambda: pl.DataFrame({"symbol": symbols}))
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 10, 23, 9, 32))
    stamps = [datetime(2026, 10, 23, 9, 31), datetime(2026, 10, 23, 9, 32)]
    provider = SimpleNamespace(get_klines=lambda *_a, **_k: pl.DataFrame({
        "symbol": [symbols[1]] * 2, "datetime": stamps,
        **{key: [10., 10.] for key in ("open", "high", "low", "close", "volume", "amount")},
    }))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda *_: (provider, "rustdx"))
    try:
        result = (
            kline_gaps.sync_missing(repo, "1m", day, day) if mode == "missing"
            else kline_periods.sync_period(repo, "1m", datetime(2026, 10, 23, 9, 31), stamps[-1], symbols=symbols)
        )
        assert result["rows"] == 2 and result["remaining"] == 2
        assert "error" not in result
        assert result["skipped_symbols"] == ["301110.SZ"]
        report = pl.read_csv(tmp_path / result["gap_report"])
        assert report["symbol"].to_list() == ["301110.SZ"]
        assert report["missing_count"].to_list() == [2]
        assert report["first_missing"].to_list() == [stamps[0].isoformat()]
        assert report["last_missing"].to_list() == [stamps[1].isoformat()]
        assert "2行" in result["warning"]
    finally:
        repo.db.close()


def test_gap_report_write_failure_is_explicit(tmp_path, monkeypatch):
    day = datetime(2026, 10, 23, 9, 31)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    (tmp_path / "kline_gap_reports").write_text("blocked")
    with pytest.raises(FileExistsError):
        kline_gaps.record_unfilled(
            repo, {"period": "1m", "source": "rustdx"},
            pl.DataFrame({"symbol": ["301110.SZ"], "datetime": [day]}),
            reason="source_missing",
        )
