"""Required malformed pages must fail rather than publish a partial history."""
from datetime import datetime, timedelta

import pytest

from app.plugins.rustdx.provider import RustdxProvider


@pytest.mark.parametrize("fault", ["repeat", "missing_time", "partly_missing_time"])
@pytest.mark.parametrize("frequency", ["1m", "1d"])
def test_required_malformed_page_is_not_published(fault, frequency, caplog):
    end = datetime(2026, 10, 28, 15)
    if frequency == "1m":
        stamps = [
            end - timedelta(days=day, minutes=minute)
            for day in range(12) for minute in range(120)
        ]
    else:
        stamps = [end - timedelta(days=day) for day in range(1600)]
    rows = [
        {"datetime": stamp, "open": 10., "high": 11., "low": 9.,
         "close": 10., "vol": 100., "amount": 1000.}
        for stamp in stamps
    ]
    calls = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def bars(self, symbol, *, start, offset, **_):
            calls.append(start)
            if start == 0:
                return rows[:800]
            if fault == "repeat":
                return rows[:800]
            page = [dict(row) for row in rows[800:1600]]
            for row in page if fault == "missing_time" else page[:1]:
                row.pop("datetime")
            return page

    frame = RustdxProvider(client_factory=Client).get_klines(
        ["600000.SH"], stamps[-1] - timedelta(days=1), end, freq=frequency,
    )
    assert frame.is_empty()
    assert "600000.SH" in caplog.text and "分页" in caplog.text
    assert calls == [0, 800]
