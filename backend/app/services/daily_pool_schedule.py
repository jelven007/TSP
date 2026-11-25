"""Beijing trading-day batches shared by single-instance and Full workers."""
from datetime import datetime, timedelta

from app.market_time import as_cn_datetime, cn_now, published_trading_day

SLOTS = ("08:30", "09:00")


def due_slot(now=None):
    now = as_cn_datetime(now or cn_now())
    if published_trading_day(now.date()) is not True or (now.hour, now.minute) < (8, 30):
        return None
    return SLOTS[0] if now.hour < 9 else SLOTS[1]


def morning_scan_slot(now=None):
    """Scan each morning batch once; retries remain inside the premarket window."""
    now = as_cn_datetime(now or cn_now())
    return due_slot(now) if (now.hour, now.minute) < (9, 30) else None


def scheduled_for(day, slot):
    from app.market_time import CN_TZ
    return datetime.combine(day, datetime.strptime(slot, "%H:%M").time(), tzinfo=CN_TZ).isoformat()


def next_check(now=None):
    """Keep minute retries without sleeping past a pool or post-close boundary."""
    now = as_cn_datetime(now or cn_now())
    candidates = [now + timedelta(minutes=1)]
    if published_trading_day(now.date()) is True:
        candidates.extend(datetime.fromisoformat(scheduled_for(now.date(), slot))
                          for slot in (*SLOTS, "16:00", "20:00", "21:00", "22:00")
                          if datetime.fromisoformat(scheduled_for(now.date(), slot)) > now)
    return min(candidates)


def batch_pool(pool, day, slot):
    return {**pool, "generation_slot": slot, "scheduled_for": scheduled_for(day, slot),
            "reference_policy": "premarket_previous_trade_day"}
