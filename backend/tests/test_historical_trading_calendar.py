"""Historical calendar regression against published SSE closures; no market requests."""

from datetime import date
from unittest.mock import Mock

import pytest

from app import market_time
from app.services.kline_gaps import _trading_days


@pytest.mark.parametrize("start", [date(2020, 1, 1), date(2023, 1, 2)])
def test_missing_calendar_covers_actual_historical_request_without_provider(monkeypatch, start):
    probe = Mock(return_value=None)
    monkeypatch.setattr("app.services.data_integrity._trading_calendar", probe)
    calendar_start = start.fromordinal(start.toordinal() - start.weekday())
    days = _trading_days(calendar_start, date(2026, 10, 26))
    assert days[0] == (date(2019, 12, 30) if start.year == 2020 else date(2023, 1, 3))
    assert days[-1] == date(2026, 10, 26)
    assert date(2024, 2, 9) not in days
    assert date(2025, 10, 8) not in days
    assert date(2025, 10, 9) in days
    probe.assert_not_called()


@pytest.mark.parametrize(
    ("closed", "reopened"),
    [
        ("2019-01-01", "2019-01-02"), ("2019-02-08", "2019-02-11"),
        ("2019-04-05", "2019-04-08"), ("2019-05-03", "2019-05-06"),
        ("2019-06-07", "2019-06-10"), ("2019-09-13", "2019-09-16"),
        ("2019-10-07", "2019-10-08"),
        ("2020-01-01", "2020-01-02"), ("2020-01-31", "2020-02-03"),
        ("2020-04-06", "2020-04-07"), ("2020-05-05", "2020-05-06"),
        ("2020-06-26", "2020-06-29"), ("2020-10-08", "2020-10-09"),
        ("2021-01-01", "2021-01-04"), ("2021-02-17", "2021-02-18"),
        ("2021-04-05", "2021-04-06"), ("2021-05-05", "2021-05-06"),
        ("2021-06-14", "2021-06-15"), ("2021-09-21", "2021-09-22"),
        ("2021-10-07", "2021-10-08"),
        ("2022-01-03", "2022-01-04"), ("2022-02-04", "2022-02-07"),
        ("2022-04-05", "2022-04-06"),
        ("2022-05-04", "2022-05-05"), ("2022-06-03", "2022-06-06"),
        ("2022-09-12", "2022-09-13"), ("2022-10-07", "2022-10-10"),
        ("2023-01-02", "2023-01-03"), ("2023-01-27", "2023-01-30"),
        ("2023-04-05", "2023-04-06"), ("2023-05-03", "2023-05-04"),
        ("2023-06-23", "2023-06-26"), ("2023-10-06", "2023-10-09"),
        ("2024-01-01", "2024-01-02"), ("2024-02-09", "2024-02-19"),
        ("2024-04-05", "2024-04-08"), ("2024-05-03", "2024-05-06"),
        ("2024-06-10", "2024-06-11"), ("2024-09-17", "2024-09-18"),
        ("2024-10-07", "2024-10-08"),
        ("2025-01-01", "2025-01-02"), ("2025-02-04", "2025-02-05"),
        ("2025-04-04", "2025-04-07"),
        ("2025-05-05", "2025-05-06"), ("2025-06-02", "2025-06-03"),
        ("2025-10-08", "2025-10-09"),
    ],
)
def test_official_historical_holidays_and_reopening(closed, reopened):
    assert market_time.published_trading_day(date.fromisoformat(closed)) is False
    assert market_time.published_trading_day(date.fromisoformat(reopened)) is True


@pytest.mark.parametrize(
    "day", ["2019-05-05", "2020-01-19", "2021-09-18", "2022-10-08",
            "2023-01-28", "2024-02-18", "2025-09-28"],
)
def test_administrative_makeup_weekends_remain_closed(day):
    assert market_time.published_trading_day(date.fromisoformat(day)) is False


@pytest.mark.parametrize("day", [date(2018, 12, 31), date(2027, 1, 4)])
def test_unpublished_year_still_requires_provider_evidence(monkeypatch, day):
    monkeypatch.setattr("app.services.data_integrity._trading_calendar", lambda: None)
    assert market_time.published_trading_day(day) is None
    assert market_time.published_trading_days(day.year) is None
    with pytest.raises(ValueError, match="缺少可靠交易日历"):
        _trading_days(day, day)


def test_unpublished_year_can_use_existing_provider_calendar(monkeypatch):
    evidence = {date(2018, 1, 2), date(2018, 1, 3), date(2018, 1, 5)}
    monkeypatch.setattr("app.services.data_integrity._trading_calendar", lambda: evidence)
    assert _trading_days(date(2018, 1, 2), date(2018, 1, 5)) == sorted(evidence)


def test_provider_recent_year_does_not_certify_older_unknown_range(monkeypatch):
    monkeypatch.setattr(
        "app.services.data_integrity._trading_calendar",
        lambda: {date(2026, 10, 23), date(2026, 10, 26)},
    )
    with pytest.raises(ValueError, match="缺少可靠交易日历"):
        _trading_days(date(2018, 1, 2), date(2018, 1, 5))
