"""采集接口、源端时钟与交易日门控回归; 不访问外部行情。"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import depth_service, kline_sync, quote_service, trading_day


def test_latest_passes_explicit_universe_to_required_parameter():
    calls = []

    class Provider:
        def get_intraday_latest(self, symbols, count=3):
            calls.append((symbols, count))
            return pl.DataFrame({
                "symbol": symbols,
                "datetime": [datetime(2026, 10, 9, 14, 59)] * len(symbols),
                "close": [10.0] * len(symbols),
            })

    frame, _ = kline_sync.fetch_intraday_custom_latest(
        Provider(), "required_symbols", symbols=["600000.SH"], count=2,
    )
    assert calls == [(["600000.SH"], 2)]
    assert frame["symbol"].to_list() == ["600000.SH"]


def test_latest_empty_universe_does_not_request_all_stocks():
    method = Mock(side_effect=AssertionError("empty universe must not fetch"))
    frame, requests = kline_sync.fetch_intraday_custom_latest(
        SimpleNamespace(get_intraday_latest=method), "custom", symbols=[],
    )
    assert frame.is_empty()
    assert requests == 0
    method.assert_not_called()


def test_latest_legacy_count_only_provider_is_called_once_and_scoped():
    method = Mock(return_value=pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"],
        "datetime": [datetime(2026, 10, 9, 14, 59)] * 2,
    }))

    class Provider:
        def get_intraday_latest(self, count=3):
            return method(count=count)

    frame, _ = kline_sync.fetch_intraday_custom_latest(
        Provider(), "legacy", symbols=["600000.SH"],
    )
    assert frame["symbol"].to_list() == ["600000.SH"]
    method.assert_called_once_with(count=3)


def test_provider_type_error_is_not_retried_as_legacy():
    calls = []

    class Provider:
        def get_intraday_latest(self, symbols, count=3):
            calls.append(symbols)
            raise TypeError("internal error after request")

    frame, requests = kline_sync.fetch_intraday_custom_latest(
        Provider(), "broken", symbols=["600000.SH"],
    )
    assert frame.is_empty()
    assert requests == 0
    assert calls == [["600000.SH"]]


def test_rustdx_records_distinguish_receive_and_source_clocks(monkeypatch):
    from app.plugins.rustdx import provider as provider_module

    ticks = iter([1000.0, 1000.5])
    monkeypatch.setattr(provider_module.time, "time", lambda: next(ticks))
    client = SimpleNamespace(quotes=lambda symbols: [{
        "market": 1, "code": "600000", "price": 10, "last_close": 9,
        "servertime": "09:25:00",
    }])
    provider = provider_module.RustdxProvider(client_factory=lambda: client)
    row = provider._quote_rows(["600000.SH"], include_names=False)[0]
    assert row["request_started_at"] == 1_000_000
    assert row["received_at"] == row["timestamp"] == 1_000_500
    assert row["timestamp_basis"] == "received"
    assert row["source_timestamp"] is None
    assert row["source_time"] == "09:25:00"


@pytest.mark.parametrize("hour", [9, 15])
def test_published_holiday_blocks_without_source_probe(monkeypatch, hour):
    fail = Mock(side_effect=AssertionError("holiday must not probe"))
    monkeypatch.setattr(trading_day, "_probe_fuyao", fail)
    monkeypatch.setattr(trading_day, "_probe_tickflow", fail)
    trading_day.reset_cache()
    assert trading_day.is_trading_day(datetime(2026, 10, 1, hour, tzinfo=CN_TZ)) is False


def test_calendar_uses_beijing_date_for_utc_input():
    # UTC Sunday night is already Monday's A-share session.
    assert trading_day.is_trading_day(
        datetime(2026, 10, 11, 23, tzinfo=UTC),
    ) is True


@pytest.mark.parametrize("verdict", [False, None])
def test_depth_finalization_requires_confirmed_trading_day(monkeypatch, verdict):
    now = datetime(2026, 10, 9, 15, 2, tzinfo=CN_TZ)
    monkeypatch.setattr(depth_service, "cn_now", lambda: now)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: verdict)
    svc = depth_service.DepthService()
    monkeypatch.setattr(svc, "_has_capability", lambda: True)
    fetch = Mock()
    monkeypatch.setattr(svc, "_fetch_and_seal", fetch)
    svc.finalize()
    fetch.assert_not_called()


def test_depth_boot_does_not_finalize_before_scheduled_close(monkeypatch):
    monkeypatch.setattr(
        depth_service, "cn_now", lambda: datetime(2026, 10, 9, 9, 10, tzinfo=CN_TZ),
    )
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    svc = depth_service.DepthService()
    monkeypatch.setattr(svc, "_has_capability", lambda: True)
    monkeypatch.setattr(svc, "_persisted_for_date", lambda day: False)
    finalize = Mock()
    monkeypatch.setattr(svc, "finalize", finalize)
    svc.boot_check()
    finalize.assert_not_called()


def test_depth_finalize_runs_after_scheduled_close(monkeypatch):
    monkeypatch.setattr(
        depth_service, "cn_now", lambda: datetime(2026, 10, 9, 15, 2, tzinfo=CN_TZ),
    )
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: True)
    monkeypatch.setattr(
        "app.services.preferences.get_depth_finalize_time",
        lambda: {"hour": 15, "minute": 2},
    )
    svc = depth_service.DepthService()
    monkeypatch.setattr(svc, "_has_capability", lambda: True)
    fetch = Mock()
    monkeypatch.setattr(svc, "_fetch_and_seal", fetch)
    svc.finalize()
    fetch.assert_called_once_with(persist=True)


def test_unknown_day_does_not_run_scheduled_pipeline(monkeypatch):
    from app.jobs import daily_pipeline

    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: None)
    run = Mock()
    monkeypatch.setattr(daily_pipeline, "_run_tracked", run)
    daily_pipeline._scheduled_pipeline_task(lambda: None)
    run.assert_not_called()


def test_received_clock_cannot_confirm_close_or_history(tmp_path, monkeypatch):
    from app.services import data_integrity

    day = date(2026, 10, 9)
    monkeypatch.setattr(quote_service, "cn_today", lambda: day)
    ts = int(datetime(2026, 10, 9, 15, 2, tzinfo=CN_TZ).timestamp() * 1000)
    record = {
        "symbol": "600000.SH", "last_price": 10.0, "open": 9.8, "high": 10.1,
        "low": 9.7, "volume": 1000.0, "amount": 10000.0,
        "timestamp": ts, "timestamp_basis": "received", "source_timestamp": None,
    }
    part = tmp_path / "date=2026-10-09"
    part.mkdir()
    frame = quote_service.QuoteService._build_daily([record])
    frame.write_parquet(part / "part.parquet")
    assert data_integrity._partition_is_snapshot(day, part, ts) is True


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        ("2026-01-01", False), ("2026-01-05", True),
        ("2026-02-14", False), ("2026-02-16", False), ("2026-02-23", False),
        ("2026-02-24", True), ("2026-02-28", False),
        ("2026-04-06", False), ("2026-04-07", True),
        ("2026-05-05", False), ("2026-05-06", True), ("2026-05-09", False),
        ("2026-06-19", False), ("2026-06-22", True),
        ("2026-09-20", False), ("2026-09-25", False), ("2026-09-28", True),
        ("2026-10-07", False), ("2026-10-08", True), ("2026-10-10", False),
        ("2027-01-04", None),
    ],
)
def test_published_calendar_boundaries(day, expected):
    from app.market_time import published_trading_day

    assert published_trading_day(date.fromisoformat(day)) is expected


def test_depth_does_not_fetch_today_to_fill_yesterdays_partition(monkeypatch):
    monkeypatch.setattr(
        depth_service, "cn_now", lambda: datetime(2026, 10, 9, 15, 2, tzinfo=CN_TZ),
    )
    svc = depth_service.DepthService()
    svc._repo = SimpleNamespace(get_enriched_latest=lambda: (
        pl.DataFrame({"symbol": ["600000.SH"], "signal_limit_up": [True]}),
        date(2026, 10, 8),
    ))
    fetch = Mock(side_effect=AssertionError("old date must not fetch"))
    monkeypatch.setattr(svc, "_call_depth_batch", fetch)
    svc._fetch_and_seal(persist=True)
    fetch.assert_not_called()
    assert svc._persisted_date is None


@pytest.mark.parametrize("verdict", [False, None])
def test_scheduled_instruments_does_not_run_on_unconfirmed_day(monkeypatch, verdict):
    from app.jobs import daily_pipeline

    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: verdict)
    run = Mock()
    monkeypatch.setattr(daily_pipeline, "_run_tracked", run)
    daily_pipeline._scheduled_instruments_task(lambda: None)
    run.assert_not_called()


def test_depth_poll_skips_confirmed_holiday(monkeypatch):
    monkeypatch.setattr(trading_day, "is_trading_day", lambda now=None: False)
    svc = depth_service.DepthService()
    count = Mock(side_effect=AssertionError("holiday must not start polling"))
    monkeypatch.setattr(svc, "_count_limit_stocks", count)
    svc._poll_once()
    count.assert_not_called()


def test_latest_invalid_symbol_column_is_isolated():
    class Provider:
        def get_intraday_latest(self, symbols, count=3):
            return pl.DataFrame({"datetime": [datetime(2026, 10, 9, 10)]})

    frame, requests = kline_sync.fetch_intraday_custom_latest(
        Provider(), "invalid", symbols=["600000.SH"],
    )
    assert frame.is_empty()
    assert requests == 0
