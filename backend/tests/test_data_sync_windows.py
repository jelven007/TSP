"""User-facing windows also govern the actual collectors, including final bars."""
from datetime import UTC, datetime, timedelta

import pytest

from app.market_time import CN_TZ, in_intraday_polling_window
from app.services import kline_periods as periods
from app.services import minute_refresh, quote_service

WINDOWS = {
    "1m": [["09:30", "11:30"], ["13:00", "15:00"]],
    "5m": [["09:36", "11:31"], ["13:06", "15:01"]],
    "10m": [["09:42", "11:32"], ["13:12", "15:02"]],
    "15m": [["09:48", "11:33"], ["13:18", "15:03"]],
}


@pytest.mark.parametrize("period", WINDOWS)
def test_each_period_ignores_legacy_windows_but_preserves_enabled_interval(tmp_path, monkeypatch, period):
    monkeypatch.setattr(periods.preferences, "_path", lambda: tmp_path / "preferences.json")
    periods.preferences.save({
        f"kline_sync_{period}": {
            "enabled": False, "interval_seconds": 77,
            "windows": [["09:15", "11:45"], ["13:00", "15:30"]],
        },
    }, tmp_path)
    config = periods.get_configs(tmp_path)[period]
    assert config["windows"] == WINDOWS[period]
    assert not config["enabled"] and config["interval_seconds"] == 77
    saved = periods.save_config(period, {"enabled": True}, tmp_path)
    assert saved["windows"] == WINDOWS[period]
    config["enabled"] = True
    for start, end in WINDOWS[period]:
        lo = datetime.fromisoformat("2026-11-06T" + start).replace(tzinfo=CN_TZ)
        hi = datetime.fromisoformat("2026-11-06T" + end).replace(tzinfo=CN_TZ)
        for now, expected in [
            (lo - timedelta(seconds=1), False), (lo, True),
            (hi, True), (hi + timedelta(seconds=59), True),
            (hi + timedelta(minutes=1), False),
        ]:
            assert periods.in_sync_window(config, now, trading_day=True) is expected
            assert periods.in_sync_window(config, now.astimezone(UTC), trading_day=True) is expected
            assert not periods.in_sync_window(config, now, trading_day=False)
    assert not periods.in_sync_window(config, datetime(2026, 11, 6, 12), trading_day=True)


@pytest.mark.parametrize("stamp,expected", [
    ("09:14:59", False), ("09:15:00", True), ("11:34:59", True),
    ("11:35:00", False), ("12:59:59", False), ("13:00:00", True),
    ("15:14:59", True), ("15:15:00", False),
])
def test_quotes_stop_at_new_cutoffs_in_beijing_and_utc(stamp, expected, monkeypatch):
    now = datetime.fromisoformat("2026-11-06T" + stamp).replace(tzinfo=CN_TZ)
    assert in_intraday_polling_window(now) is expected
    assert in_intraday_polling_window(now.astimezone(UTC)) is expected
    monkeypatch.setattr(quote_service, "cn_now", lambda: now)
    assert (quote_service.QuoteService._market_phase() != "closed") is expected


@pytest.mark.parametrize("phase,stamp", [
    ("morning_final", "11:35:00"), ("close_final", "15:15:00"),
])
def test_quote_final_confirmation_cannot_retry_after_cutoff(phase, stamp, monkeypatch):
    monkeypatch.setattr(quote_service, "cn_now", lambda:
                        datetime.fromisoformat("2026-11-06T" + stamp).replace(tzinfo=CN_TZ))
    assert quote_service.QuoteService._past_final_deadline(phase)


@pytest.mark.parametrize("period", ["5m", "10m", "15m"])
def test_derived_final_minute_flushes_even_before_normal_interval(period, monkeypatch):
    now = datetime.fromisoformat("2026-11-06T" + WINDOWS[period][1][1]).replace(second=30)
    config = periods.get_configs()
    config = {p: {**cfg, "enabled": p == period} for p, cfg in config.items()}
    calls = []
    monkeypatch.setattr(periods, "cn_now", lambda: now)
    monkeypatch.setattr(periods, "get_configs", lambda: config)
    monkeypatch.setattr(periods.preferences, "get_data_sync_auto_enabled", lambda: True)
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda _: True)
    monkeypatch.setattr(periods, "_last_success", {period: now - timedelta(minutes=1)})
    monkeypatch.setattr("app.jobs.daily_pipeline._run_tracked", lambda fn, label: calls.append(label) or True)
    periods.scheduled_periods(None)
    periods.scheduled_periods(None)
    assert calls == ["kline_" + period]


def test_one_minute_final_flush_not_lost_to_a_long_interval(monkeypatch):
    now = datetime(2026, 11, 6, 15, 0, 10, tzinfo=CN_TZ)
    service = minute_refresh.MinuteRefreshService(None)
    service._state.last_round_at = (now - timedelta(minutes=1)).timestamp()
    assert service._terminal_refresh_due(now)
    service._state.last_round_at = now.timestamp()
    assert not service._terminal_refresh_due(now)
