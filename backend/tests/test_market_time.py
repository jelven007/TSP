from datetime import datetime

import pytest

from app.market_time import CN_TZ, in_intraday_polling_window


@pytest.mark.parametrize(("hour", "minute", "expected"), [
    (9, 14, False),
    (9, 15, True),
    (11, 44, True),
    (11, 45, False),
    (12, 59, False),
    (13, 0, True),
    (15, 59, True),
    (16, 0, False),
])
def test_intraday_collection_window_uses_the_unified_trading_day_period(hour, minute, expected):
    now = datetime(2026, 10, 26, hour, minute, tzinfo=CN_TZ)
    assert in_intraday_polling_window(now) is expected
