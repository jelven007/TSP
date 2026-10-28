"""Full minute collection skips only calendar-proven closed windows."""
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import kline_periods


@pytest.mark.parametrize(
    ("start", "end", "calls_expected"),
    [(datetime(2026, 5, 1), datetime(2026, 5, 5, 23, 59), 0),
     (datetime(2026, 3, 30), datetime(2026, 5, 1, 23, 59), 3),
     (datetime(2030, 5, 1), datetime(2030, 5, 5, 23, 59), 3)],
)
def test_full_minute_closed_tail_does_not_fetch_entire_universe(tmp_path, monkeypatch, start, end, calls_expected):
    symbols = [f"{i:06d}.SZ" for i in range(1, 202)]
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments=lambda: pl.DataFrame({"symbol": symbols}),
    )
    monkeypatch.setattr(kline_periods, "_period_provider", lambda _: (object(), "test"))
    monkeypatch.setattr(kline_periods, "persist_period", lambda *_: 0)
    calls, progress = [], []

    def fetch(_, period, batch, lo, hi, **kwargs):
        calls.append((batch, lo, hi))
        return pl.DataFrame()

    monkeypatch.setattr(kline_periods, "fetch_period", fetch)
    result = kline_periods.sync_period(
        repo, "1m", start, end, symbols=symbols,
        on_progress=lambda _, pct, msg, **kw: progress.append((pct, kw["stage_pct"])),
    )
    assert len(calls) == calls_expected
    assert result["rows"] == 0
    assert progress == sorted(progress) and progress[-1] == (95, 100)
