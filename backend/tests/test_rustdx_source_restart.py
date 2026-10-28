"""A changed paging source must restart the symbol without mixing old rows."""
from datetime import datetime, timedelta

import pytest

from app.plugins.rustdx.client import RustdxError, RustdxSourceChangedError
from app.plugins.rustdx.provider import RustdxProvider


def minute_rows():
    stamps = []
    day = datetime(2026, 4, 10)
    while len(stamps) < 1600:
        if day.weekday() < 5:
            stamps.extend(
                day + timedelta(minutes=base + minute)
                for base in (780, 570) for minute in range(120, 0, -1)
            )
        day -= timedelta(days=1)
    return [{"datetime": stamp, "open": 10., "high": 11., "low": 9.,
             "close": 10., "vol": 100., "amount": 1000.}
            for stamp in stamps[:1600]]


class ChangingPages:
    def __init__(self, *, mismatch=False, always=False, restart_error=None):
        self.rows = minute_rows()
        self.mismatch = mismatch
        self.always = always
        self.restart_error = restart_error
        self.attempts = 0
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def bars_with_source(self, symbol, *, start, expected_source=None, **_):
        self.calls.append((symbol, start, expected_source))
        if start == 0:
            self.attempts += 1
            if self.attempts > 1 and self.restart_error:
                raise self.restart_error
        source = {"endpoint": "test:7709", "connection_generation": self.attempts}
        if start == 800 and (self.attempts == 1 or self.always):
            if not self.mismatch:
                raise RustdxSourceChangedError("源连接重建, 须从头重采")
            source["connection_generation"] += 1
        rows = [{**row, "close": 10. * self.attempts}
                for row in self.rows[start:start + 800]]
        return rows, source


@pytest.mark.parametrize("mismatch", [False, True])
@pytest.mark.parametrize("phase", ["probe", "range"])
def test_transient_source_change_restarts_symbol_from_zero_without_old_rows(mismatch, phase):
    client = ChangingPages(mismatch=mismatch)
    provider = RustdxProvider(client_factory=lambda: client)
    start = datetime(2026, 3, 30)
    end = client.rows[800]["datetime"] if phase == "probe" else datetime(2026, 5, 1)
    result = provider.get_minute(["301110.SZ"], start, end)
    expected = provider._minute_frame(
        [{**row, "close": 20.} for row in client.rows], "301110.SZ", start, end,
    )
    assert result.equals(expected)
    assert result["close"].unique().to_list() == [20.]
    assert client.attempts == 2
    assert len(client.calls) <= 5
    assert [source for _, offset, source in client.calls if offset == 0] == [None, None]


@pytest.mark.parametrize("frequency", ["1d", "1w"])
def test_daily_and_weekly_paging_restart_without_old_source_rows(frequency):
    client = ChangingPages()
    step = 7 if frequency == "1w" else 1
    client.rows = [
        {**client.rows[0], "datetime": datetime(2026, 4, 10) - timedelta(days=i * step)}
        for i in range(1600)
    ]
    start, end = client.rows[-1]["datetime"], datetime(2026, 5, 1)
    provider = RustdxProvider(client_factory=lambda: client)
    result = provider.get_klines(["301110.SZ"], start, end, freq=frequency)
    expected = provider._daily_frame(
        [{**row, "close": 20.} for row in client.rows], "301110.SZ", start, end,
    )
    assert result.equals(expected)
    assert client.attempts == 2
    assert len(client.calls) == 4


def test_repeated_source_changes_remain_bounded_and_reject_partial_data():
    client = ChangingPages(always=True)
    frame = RustdxProvider(client_factory=lambda: client).get_minute(
        ["301110.SZ"], datetime(2026, 3, 30), datetime(2026, 5, 1),
    )
    assert frame.is_empty()
    assert client.attempts == 2
    assert len(client.calls) == 4


def test_cancellation_during_source_restart_propagates():
    from app.services.pipeline_jobs import JobCancelledError

    client = ChangingPages(restart_error=JobCancelledError("用户取消"))
    with pytest.raises(JobCancelledError, match="用户取消"):
        RustdxProvider(client_factory=lambda: client).get_minute(
            ["301110.SZ"], datetime(2026, 3, 30), datetime(2026, 5, 1),
        )
    assert client.attempts == 2


def test_unrelated_protocol_failure_is_not_retried():
    client = ChangingPages()

    def fail(*_args, **_kwargs):
        client.attempts += 1
        raise RustdxError("协议失败")

    client.bars_with_source = fail
    frame = RustdxProvider(client_factory=lambda: client).get_minute(
        ["301110.SZ"], datetime(2026, 3, 30), datetime(2026, 5, 1),
    )
    assert frame.is_empty()
    assert client.attempts == 1
