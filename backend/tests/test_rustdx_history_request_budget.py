"""Historical windows must locate their pages without downloading the recent prefix."""
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.plugins.rustdx.client import RustdxError
from app.plugins.rustdx.provider import RustdxProvider
from app.services import kline_gaps, kline_periods


def history(frequency=8, pages=55):
    stamps = []
    day = datetime(2026, 10, 28)
    while len(stamps) < pages * 800:
        if frequency in (9, 5):
            stamps.append(day.replace(hour=15))
            day -= timedelta(days=7 if frequency == 5 else 1)
        else:
            if day.weekday() < 5:
                stamps.extend(
                    day + timedelta(minutes=base + minute)
                    for base in (780, 570) for minute in range(120, 0, -1)
                )
            day -= timedelta(days=1)
    return [
        {"datetime": stamp.isoformat(), "open": 10., "high": 11., "low": 9.,
         "close": 10. + i / 100000., "vol": None if i % 7 == 0 else 100.,
         "amount": None if i % 11 == 0 else 1000.}
        for i, stamp in enumerate(stamps[:pages * 800])
    ]


class Pages:
    def __init__(self, rows, overrides=None):
        self.rows = rows
        self.calls = []
        self.overrides = overrides or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def bars(self, symbol, *, frequency, start, offset):
        self.calls.append((symbol, frequency, start, offset))
        page = start // 800
        if page in self.overrides:
            value = self.overrides[page]
            if isinstance(value, BaseException):
                raise value
            return value
        return self.rows[start:start + offset]


@pytest.mark.parametrize(
    ("symbol", "period", "frequency", "asset"),
    [("600000.SH", "1m", 8, "stock"), ("000001.SH", "1m", 8, "index"),
     ("510300.SH", "1m", 8, "etf"), ("600000.SH", "5m", 0, "stock"),
     ("600000.SH", "15m", 1, "stock"), ("600000.SH", "1d", 9, "stock"),
     ("000001.SH", "1d", 9, "index"), ("510300.SH", "1d", 9, "etf"),
     ("600000.SH", "1w", 5, "stock")],
)
def test_old_window_locates_pages_and_preserves_content(symbol, period, frequency, asset):
    raw = history(frequency)
    start = datetime.fromisoformat(raw[27 * 800 + 200]["datetime"])
    end = datetime.fromisoformat(raw[26 * 800]["datetime"])
    # Daily/weekly APIs include the whole end date even with a midnight bound.
    if frequency in (9, 5):
        end = end.replace(hour=0)
    else:
        start = (start - timedelta(hours=8)).replace(tzinfo=UTC)
        end = (end - timedelta(hours=8)).replace(tzinfo=UTC)
    client = Pages(raw)
    provider = RustdxProvider(client_factory=lambda: client)
    result = provider.get_klines([symbol], start, end, asset_type=asset, freq=period)
    expected = (
        provider._daily_frame(raw, symbol, start, end) if frequency in (9, 5)
        else provider._minute_frame(raw, symbol, start, end, frequency)
    )
    assert result.equals(expected)
    assert len(client.calls) <= 14
    assert len({call[2] for call in client.calls}) == len(client.calls)


def test_missing_repair_locates_each_gap_and_preserves_existing_data(tmp_path, monkeypatch):
    symbol = "600000.SH"
    raw = history()
    day = date(2026, 4, 2)
    client = Pages(raw)
    provider = RustdxProvider(client_factory=lambda: client)
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: pl.DataFrame({"symbol": [symbol]}),
    )
    monkeypatch.setattr(kline_gaps.instrument_sync, "load_current_stock_pool", lambda *a, **kw: SimpleNamespace(symbols=[symbol]))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (provider, "rustdx"))
    monkeypatch.setattr(kline_periods, "refresh_period", lambda *_: None)
    monkeypatch.setattr("app.market_time.cn_now", lambda: datetime(2026, 10, 28, 16))
    expected = provider._minute_frame(raw, symbol, datetime.combine(day, datetime.min.time()), datetime.combine(day, datetime.max.time()))
    missing = [datetime(2026, 4, 2, 9, 32), datetime(2026, 4, 2, 9, 33), datetime(2026, 4, 2, 14, 59)]
    existing = expected.filter(~pl.col("datetime").is_in(missing)).with_columns(pl.lit(99.).alias("close"))
    kline_periods.persist_period(repo, "1m", existing)
    result = kline_gaps.sync_missing(repo, "1m", day, day)
    stored = pl.read_parquet(sorted((tmp_path / "kline_minute").glob("date=*/*.parquet"))).sort("symbol", "datetime")
    assert result["rows"] == result["missing_before"] == 3
    assert result["remaining"] == 0 and "error" not in result
    assert result["requests"] == 2  # API windows, separate from protocol pages.
    assert stored.filter(~pl.col("datetime").is_in(missing)).equals(existing)
    assert stored.filter(pl.col("datetime").is_in(missing)).equals(expected.filter(pl.col("datetime").is_in(missing)))
    assert len(client.calls) <= 28
    client.calls.clear()
    repeat = kline_gaps.sync_missing(repo, "1m", day, day)
    assert repeat["requests"] == repeat["rows"] == 0
    assert client.calls == []


@pytest.mark.parametrize("page_limit", ["1", "4", "invalid", "999"])
def test_page_limit_empty_and_short_history_remain_bounded(monkeypatch, page_limit):
    monkeypatch.setenv("RUSTDX_MAX_PAGES", page_limit)
    raw = history(pages=6)[:4 * 800 + 73]
    client = Pages(raw)
    provider = RustdxProvider(client_factory=lambda: client)
    start = datetime.fromisoformat(raw[-1]["datetime"])
    end = datetime.fromisoformat(raw[-73]["datetime"])
    result = provider.get_minute(["600000.SH"], start, end)
    limit = 1 if page_limit == "1" else 4 if page_limit == "4" else 64 if page_limit == "invalid" else 80
    expected = provider._minute_frame(raw[:limit * 800], "600000.SH", start, end)
    assert result.equals(expected)
    assert len(client.calls) <= limit
    assert all(call[2] < limit * 800 for call in client.calls)


@pytest.mark.parametrize("bad", ["repeat", "no_time", "error"])
def test_bad_probe_falls_back_without_losing_target_or_hiding_target_failure(bad):
    raw = history(pages=40)
    replacement = raw[:800] if bad == "repeat" else [{"close": 10.}] * 800 if bad == "no_time" else RustdxError("injected probe failure")
    client = Pages(raw, {32: replacement})
    provider = RustdxProvider(client_factory=lambda: client)
    start = datetime.fromisoformat(raw[27 * 800 + 100]["datetime"])
    end = datetime.fromisoformat(raw[26 * 800 + 100]["datetime"])
    result = provider.get_minute(["600000.SH"], start, end)
    assert result.equals(provider._minute_frame(raw, "600000.SH", start, end))
    assert len({call[2] for call in client.calls}) == len(client.calls)
    if bad == "error":
        # A failed target must not publish the incomplete pages of that security.
        client = Pages(raw, {26: RustdxError("injected target failure")})
        assert RustdxProvider(client_factory=lambda: client).get_minute(["600000.SH"], start, end).is_empty()


def test_separate_calls_observe_updated_historical_rows():
    raw = history()
    client = Pages(raw)
    provider = RustdxProvider(client_factory=lambda: client)
    stamp = datetime.fromisoformat(raw[26 * 800 + 100]["datetime"])
    first = provider.get_minute(["600000.SH"], stamp, stamp)
    raw[26 * 800 + 100]["close"] = 88.
    second = provider.get_minute(["600000.SH"], stamp, stamp)
    assert first["close"][0] != second["close"][0] == 88.


@pytest.mark.parametrize("kind", ["empty", "latest", "suspended", "ascending"])
def test_latest_empty_and_irregular_history_keep_exact_time_filter(kind):
    raw = history()
    if kind == "empty":
        raw = []
    elif kind == "suspended":
        raw = raw[:3000] + raw[11000:19000] + raw[25000:]
    end = datetime(2026, 10, 28, 15) if kind in ("empty", "latest") else datetime.fromisoformat(raw[21 * 800]["datetime"])
    start = end - timedelta(minutes=3) if kind in ("empty", "latest") else datetime.fromisoformat(raw[22 * 800 + 100]["datetime"])
    client = Pages(raw)
    if kind == "ascending":
        # TDX may return each page from older to newer, while offsets still
        # advance backwards. Page location must depend on timestamps, not order.
        client.overrides = {page: list(reversed(raw[page * 800:(page + 1) * 800])) for page in range(55)}
    provider = RustdxProvider(client_factory=lambda: client)
    assert provider.get_minute(["600000.SH"], start, end).equals(provider._minute_frame(raw, "600000.SH", start, end))
    assert len(client.calls) <= (1 if kind in ("empty", "latest") else 14)


def test_cancellation_during_probe_is_not_treated_as_unavailable_history():
    from app.services.pipeline_jobs import JobCancelledError
    raw = history()
    client = Pages(raw, {32: JobCancelledError("用户取消")})
    start = datetime.fromisoformat(raw[27 * 800]["datetime"])
    end = datetime.fromisoformat(raw[26 * 800]["datetime"])
    with pytest.raises(JobCancelledError, match="用户取消"):
        RustdxProvider(client_factory=lambda: client).get_minute(["600000.SH"], start, end)


def test_old_adjustment_event_uses_its_previous_close_after_page_location():
    raw = history(9, pages=6)
    index = 3 * 800 + 100
    event_day = datetime.fromisoformat(raw[index]["datetime"]).date()
    client = Pages(raw)
    client.xdxr = lambda _: [{
        "year": event_day.year, "month": event_day.month, "day": event_day.day,
        "category": 1, "fenhong": 1., "songzhuangu": 0., "peigu": 0.,
    }]
    provider = RustdxProvider(client_factory=lambda: client)
    frame = provider.get_adj_factors(
        ["600000.SH"], datetime.combine(event_day, datetime.min.time()),
        datetime.combine(event_day, datetime.max.time()), strict=True,
    )
    previous = raw[index + 1]["close"]
    reference = round(previous - .1, 2)
    assert frame["trade_date"].to_list() == [event_day]
    assert frame["ex_factor"][0] == pytest.approx(previous / reference)
