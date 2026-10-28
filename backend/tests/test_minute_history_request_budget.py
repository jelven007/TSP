"""A monthly fetch must not rescan recent TDX history for every five days."""
from datetime import datetime, timedelta
from itertools import pairwise
from math import ceil
from types import SimpleNamespace

import polars as pl
import pytest

from app.plugins.rustdx.provider import RustdxProvider
from app.services import kline_periods


@pytest.mark.parametrize("end", [datetime(2026, 1, 31, 23, 59, 59), datetime(2026, 2, 1, 23, 59, 59)])
def test_month_history_reads_recent_pages_once_and_preserves_all_bars(tmp_path, monkeypatch, end):
    start = datetime(2026, 1, 1)
    raw = []
    day = start
    while day <= datetime(2026, 3, 1):
        if day.weekday() < 5:
            for base in (570, 780):
                for minute in range(1, 121):
                    raw.append({
                        "datetime": (day + timedelta(minutes=base + minute)).isoformat(),
                        "open": 10., "high": 11., "low": 9., "close": 10.5,
                        "vol": 100., "amount": 1050.,
                    })
        day += timedelta(days=1)
    latest_first = list(reversed(raw))
    requests = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def bars(self, symbol, *, frequency, start, offset):
            requests.append((symbol, frequency, start, offset))
            return latest_first[start:start + offset]

    provider = RustdxProvider(client_factory=Client)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (provider, "rustdx"))
    monkeypatch.setattr(kline_periods, "refresh_period", lambda *_: None)
    progress = []
    result = kline_periods.sync_period(
        repo, "1m", start, end, symbols=["600000.SH"],
        on_progress=lambda _, pct, msg, **kw: progress.append((pct, kw["stage_pct"])),
    )
    expected = provider._minute_frame(raw, "600000.SH", start, end)
    stored = pl.read_parquet(sorted((tmp_path / "kline_minute").glob("date=*/*.parquet"))).sort("symbol", "datetime")
    assert stored.equals(expected)
    assert result["rows"] == expected.height
    assert result["start"] == start.isoformat() and result["end"] == end.isoformat()
    assert result["price_basis"] == "raw"
    assert progress[-1] == (95, 100)
    assert [pct for pct, _ in progress] == sorted(pct for pct, _ in progress)
    # One complete lookback is sufficient for a single closed calendar month.
    # This budget catches redundant scans regardless of network latency.
    assert len(requests) <= ceil(len(raw) / 800)


def test_long_history_keeps_bounded_windows_symbol_batches_and_exact_boundaries(tmp_path, monkeypatch):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (object(), "test"))
    monkeypatch.setattr(kline_periods, "persist_period", lambda *_: 0)
    windows = []

    def fetch(provider, period, symbols, start, end, **kwargs):
        windows.append((tuple(symbols), start, end))
        return pl.DataFrame()

    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    start, end = datetime(2026, 1, 1, 10, 5), datetime(2026, 3, 15, 14, 50)
    symbols = [f"{i:06d}.SZ" for i in range(1, 202)]
    kline_periods.sync_period(repo, "1m", start, end, symbols=symbols)
    assert all(len(batch) <= 100 for batch, _, _ in windows)
    for offset in range(3):
        own = windows[offset::3]
        assert own[0][1] == start and own[-1][2] == end
        assert all((hi.date() - lo.date()).days < 32 for _, lo, hi in own)
        assert all(left[2] + timedelta(microseconds=1) == right[1] for left, right in pairwise(own))
        assert all(batch == tuple(symbols[offset * 100:(offset + 1) * 100]) for batch, _, _ in own)
