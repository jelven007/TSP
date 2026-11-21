"""One clock for next-session eligibility and its source-day evidence."""
from datetime import time

SELECTION_HOUR = 20
SELECTION_TIME = time(SELECTION_HOUR)
SELECTION_TIME_TEXT = "20:00"
S02_ID = "custom_mvxxcp88"
S03_ID = "custom_mvxxcs03"


def window_for(strategy_id=None):
    """Inclusive submission bounds, in Beijing time."""
    return (time(21), time(22)) if strategy_id == S03_ID else (SELECTION_TIME, time(21))


def window_text(strategy_id=None):
    return tuple(value.strftime("%H:%M:%S") for value in window_for(strategy_id))
