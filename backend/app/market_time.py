"""A股市场时间工具 — 固定北京时间 (UTC+8, 无夏令时)。

服务器/容器本地时区不可靠 (python:slim 镜像默认 UTC), 交易时段判断、
实时行情落盘日期等必须显式使用北京时间, 否则 Docker 部署时轮询窗口
与真实交易时段完全错开 (北京 9:15-15:05 = UTC 1:15-7:05)。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from functools import lru_cache

CN_TZ = timezone(timedelta(hours=8))

# A 股交易时段 (北京时间): 上午 9:30-11:30 (120 分钟) + 下午 13:00-15:00 (120 分钟) = 240 分钟
_TRADING_TOTAL_MINUTES = 240
_MORNING_START = dt_time(9, 30)
_MORNING_END = dt_time(11, 30)
_AFTERNOON_START = dt_time(13, 0)
_AFTERNOON_END = dt_time(15, 0)

# 盘中常驻拉取窗口: 竞价预热从 09:15 开始, 收盘定版延续到 15:15。
# 结束边界采用半开区间, 11:30 与 15:15 到点即停。
_INTRADAY_POLL_MORNING_START = dt_time(9, 15)
_INTRADAY_POLL_MORNING_END = dt_time(11, 30)
_INTRADAY_POLL_AFTERNOON_START = dt_time(13, 0)
_INTRADAY_POLL_AFTERNOON_END = dt_time(15, 15)

# 盘前集合竞价采样窗口: 09:15-09:25 (半开区间)。覆盖竞价指示价的持续变化;
# 09:25 撮合终态由独立的 auction_snapshot 采集, 不在此窗口内。
_PREOPEN_AUCTION_START = dt_time(9, 15)
_PREOPEN_AUCTION_END = dt_time(9, 25)


def cn_now() -> datetime:
    """当前北京时间 (带时区)。"""
    return datetime.now(CN_TZ)


def cn_today() -> date:
    """当前北京日期。"""
    return datetime.now(CN_TZ).date()


def as_cn_datetime(value: datetime) -> datetime:
    """显式时区转北京; 兼容既有北京墙钟 naive 输入。"""
    return value.astimezone(CN_TZ) if value.tzinfo else value.replace(tzinfo=CN_TZ)


# 上证公告(2025)45号, 2025-12-22 发布; 覆盖 2026 全年沪深 A 股。
# https://www.sse.com.cn/disclosure/dealinstruc/closed/
# 不使用行政调休工作日: 周六/周日即使补班也不交易。
TRADING_CALENDAR_VERSION = "SSE-2026-20251222"
_PUBLISHED_HOLIDAYS = {
    2026: (
        (date(2026, 1, 1), date(2026, 1, 3)),
        (date(2026, 2, 15), date(2026, 2, 23)),
        (date(2026, 4, 4), date(2026, 4, 6)),
        (date(2026, 5, 1), date(2026, 5, 5)),
        (date(2026, 6, 19), date(2026, 6, 21)),
        (date(2026, 9, 25), date(2026, 9, 27)),
        (date(2026, 10, 1), date(2026, 10, 7)),
    ),
}


def published_trading_day(day: date) -> bool | None:
    """版本化休市安排; 未覆盖年份返回未知, 不猜测法定假日。"""
    if day.weekday() >= 5:
        return False
    holidays = _PUBLISHED_HOLIDAYS.get(day.year)
    if holidays is None:
        return None
    return not any(start <= day <= end for start, end in holidays)


@lru_cache(maxsize=4)
def published_trading_days(year: int) -> frozenset[date] | None:
    """返回公布年份的完整交易日集合, 供无网络健康检查使用。"""
    if year not in _PUBLISHED_HOLIDAYS:
        return None
    start, end = date(year, 1, 1), date(year + 1, 1, 1)
    return frozenset(
        day for offset in range((end - start).days)
        if published_trading_day(day := start + timedelta(days=offset))
    )


def trading_calendar_provenance() -> dict:
    """Fingerprint the published schedule without probing a data provider."""
    years = sorted(_PUBLISHED_HOLIDAYS)
    content = "\n".join(
        day.isoformat()
        for year in years
        for day in sorted(published_trading_days(year) or ())
    )
    return {
        "version": TRADING_CALENDAR_VERSION,
        "covered_years": years,
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "unknown_year_policy": "provider_evidence",
    }


def in_continuous_session(now: datetime | None = None) -> bool:
    """A股连续竞价时段 (北京时间): 9:30-11:30 / 13:00-15:00, 仅工作日。"""
    now = as_cn_datetime(now or cn_now())
    return now.weekday() < 5 and (
        _MORNING_START <= now.time() <= _MORNING_END
        or _AFTERNOON_START <= now.time() <= _AFTERNOON_END
    )


def in_intraday_polling_window(now: datetime | None = None) -> bool:
    """盘中常驻拉取窗口: 09:15-11:30 / 13:00-15:15, 仅工作日。

    节假日由调用方通过交易日探针判断; 本函数只负责北京时间、周末和双时段边界。
    """
    now = as_cn_datetime(now or cn_now())
    return now.weekday() < 5 and (
        _INTRADAY_POLL_MORNING_START <= now.time() < _INTRADAY_POLL_MORNING_END
        or _INTRADAY_POLL_AFTERNOON_START <= now.time() < _INTRADAY_POLL_AFTERNOON_END
    )


def in_preopen_auction_window(now: datetime | None = None) -> bool:
    """盘前集合竞价采样窗口: 09:15-09:25 (半开区间), 仅工作日。

    节假日由调用方通过交易日探针判断; 本函数只负责北京时间、周末和边界。
    """
    now = as_cn_datetime(now or cn_now())
    return now.weekday() < 5 and _PREOPEN_AUCTION_START <= now.time() < _PREOPEN_AUCTION_END


def trading_minutes_elapsed_from_dt(dt: datetime) -> float:
    """根据北京时间 datetime 计算当日已交易分钟数。

    交易时段: 9:30-11:30 (0~120) + 13:00-15:00 (120~240)。
    - 开盘前 = 0; 午休(11:30-13:00) = 120(保持上午累计); 收盘后 = 240。
    - 非交易日(周末) = 240 (视作全天, 避免量比被折算成 0)。
    """
    dt = as_cn_datetime(dt)
    if dt.weekday() >= 5:
        return float(_TRADING_TOTAL_MINUTES)
    t = dt.time()
    if t < _MORNING_START:
        return 0.0
    if t < _MORNING_END:
        return (dt.hour * 60 + dt.minute - 9 * 60 - 30) + dt.second / 60.0
    if t < _AFTERNOON_START:
        return 120.0  # 午休, 保持上午累计
    if t < _AFTERNOON_END:
        return 120.0 + (dt.hour * 60 + dt.minute - 13 * 60) + dt.second / 60.0
    return float(_TRADING_TOTAL_MINUTES)


def trading_minutes_elapsed() -> float:
    """当前已交易分钟数 (基于服务端北京时间)。

    量比折算的兜底: 当行情 timestamp 缺失时用服务端时间。
    优先使用 trading_minutes_elapsed_from_ts (行情真实时间, 更准)。
    """
    return trading_minutes_elapsed_from_dt(cn_now())


def trading_minutes_elapsed_from_ts(ts_ms: int | float | None) -> float:
    """从行情时间戳(毫秒)计算当日已交易分钟数。

    优先使用此函数: 行情 timestamp 是真实成交时间, 比服务端时间更准
    (服务端时间含网络/限流延迟)。

    Args:
        ts_ms: 毫秒级 Unix 时间戳 (TickFlow SDK quote.timestamp / kline.timestamp)

    Returns:
        已交易分钟数 (0~240)。timestamp 为 None/无效时返回 240 (视作全天,
        避免量比被折算成 0)。
    """
    if not ts_ms:
        return float(_TRADING_TOTAL_MINUTES)
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=CN_TZ)
    except (ValueError, TypeError, OSError):
        return float(_TRADING_TOTAL_MINUTES)
    return trading_minutes_elapsed_from_dt(dt)
