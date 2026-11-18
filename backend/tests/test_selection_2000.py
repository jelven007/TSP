from datetime import date, datetime

import pytest

from app.market_time import CN_TZ
from app.services.daily_selections import due_window


@pytest.mark.parametrize("clock, expected", [
    (datetime(2026, 11, 13, 19, 59, 59, tzinfo=CN_TZ), None),
    (datetime(2026, 11, 13, 20, tzinfo=CN_TZ), (date(2026, 11, 13), date(2026, 11, 16))),
    (datetime(2026, 11, 14, 12, tzinfo=CN_TZ), (date(2026, 11, 13), date(2026, 11, 16))),
    (datetime(2026, 11, 16, 9, 29, 59, tzinfo=CN_TZ), (date(2026, 11, 13), date(2026, 11, 16))),
    (datetime(2026, 11, 16, 9, 30, tzinfo=CN_TZ), None),
])
def test_selection_recovers_only_after_2000_before_target_open(clock, expected):
    assert due_window(clock) == expected
