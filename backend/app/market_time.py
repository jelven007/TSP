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

# 交易日数据采集窗口: 竞价预热从 09:15 开始, 收盘定版延续到 16:00。
# 结束边界采用半开区间, 11:45 与 16:00 到点即停。
TRADING_DAY_COLLECTION_WINDOWS = (("09:15", "11:45"), ("13:00", "16:00"))
_INTRADAY_POLL_MORNING_START = dt_time(9, 15)
_INTRADAY_POLL_MORNING_END = dt_time(11, 45)
_INTRADAY_POLL_AFTERNOON_START = dt_time(13, 0)
_INTRADAY_POLL_AFTERNOON_END = dt_time(16, 0)

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


# 上交所公布的 2019-2026 全年休市安排; 历史来源及修订见各年份。
# 2026: 上证公告(2025)45号, 2025-12-22 发布。
# https://www.sse.com.cn/disclosure/dealinstruc/closed/
# 不使用行政调休工作日: 周六/周日即使补班也不交易。
TRADING_CALENDAR_VERSION = "SSE-2019-2026-20261026"
_PUBLISHED_HOLIDAYS = {
    # https://www.sse.com.cn/disclosure/announcement/general/c/c_20181220_4696473.shtml
    # 劳动节按修订公告扩展至 5 月 4 日, 不沿用原年度公告的仅 5 月 1 日。
    # https://www.sse.com.cn/disclosure/announcement/general/c/c_20190418_4771364.shtml
    2019: (
        (date(2019, 1, 1), date(2019, 1, 1)),
        (date(2019, 2, 4), date(2019, 2, 10)),
        (date(2019, 4, 5), date(2019, 4, 7)),
        (date(2019, 5, 1), date(2019, 5, 4)),
        (date(2019, 6, 7), date(2019, 6, 9)),
        (date(2019, 9, 13), date(2019, 9, 15)),
        (date(2019, 10, 1), date(2019, 10, 7)),
    ),
    # https://www.sse.com.cn/disclosure/announcement/general/c/c_20191220_4969627.shtml
    # 春节因疫情延长至 2 月 2 日, 1 月 31 日同样休市。
    # https://www.sse.com.cn/disclosure/announcement/general/c/c_20200127_4991582.shtml
    2020: (
        (date(2020, 1, 1), date(2020, 1, 1)),
        (date(2020, 1, 24), date(2020, 2, 2)),
        (date(2020, 4, 4), date(2020, 4, 6)),
        (date(2020, 5, 1), date(2020, 5, 5)),
        (date(2020, 6, 25), date(2020, 6, 27)),
        (date(2020, 10, 1), date(2020, 10, 8)),
    ),
    # https://www.sse.com.cn/disclosure/announcement/general/c/c_20201224_5286949.shtml
    2021: (
        (date(2021, 1, 1), date(2021, 1, 3)),
        (date(2021, 2, 11), date(2021, 2, 17)),
        (date(2021, 4, 3), date(2021, 4, 5)),
        (date(2021, 5, 1), date(2021, 5, 5)),
        (date(2021, 6, 12), date(2021, 6, 14)),
        (date(2021, 9, 19), date(2021, 9, 21)),
        (date(2021, 10, 1), date(2021, 10, 7)),
    ),
    # https://www.sse.com.cn/disclosure/announcement/general/c/c_20211220_5662606.shtml
    2022: (
        (date(2022, 1, 1), date(2022, 1, 3)),
        (date(2022, 1, 31), date(2022, 2, 6)),
        (date(2022, 4, 3), date(2022, 4, 5)),
        (date(2022, 4, 30), date(2022, 5, 4)),
        (date(2022, 6, 3), date(2022, 6, 5)),
        (date(2022, 9, 10), date(2022, 9, 12)),
        (date(2022, 10, 1), date(2022, 10, 7)),
    ),
    # https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20221227_5714459.shtml
    2023: (
        (date(2023, 1, 1), date(2023, 1, 2)),
        (date(2023, 1, 21), date(2023, 1, 27)),
        (date(2023, 4, 5), date(2023, 4, 5)),
        (date(2023, 4, 29), date(2023, 5, 3)),
        (date(2023, 6, 22), date(2023, 6, 24)),
        (date(2023, 9, 29), date(2023, 10, 6)),
    ),
    # https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20231226_5733941.shtml
    # 除夕 2 月 9 日也休市, 与行政法定假期起点不同。
    2024: (
        (date(2024, 1, 1), date(2024, 1, 1)),
        (date(2024, 2, 9), date(2024, 2, 17)),
        (date(2024, 4, 4), date(2024, 4, 6)),
        (date(2024, 5, 1), date(2024, 5, 5)),
        (date(2024, 6, 10), date(2024, 6, 10)),
        (date(2024, 9, 15), date(2024, 9, 17)),
        (date(2024, 10, 1), date(2024, 10, 7)),
    ),
    # https://www.sse.com.cn/disclosure/announcement/general/c/c_20241223_10767108.shtml
    2025: (
        (date(2025, 1, 1), date(2025, 1, 1)),
        (date(2025, 1, 28), date(2025, 2, 4)),
        (date(2025, 4, 4), date(2025, 4, 6)),
        (date(2025, 5, 1), date(2025, 5, 5)),
        (date(2025, 5, 31), date(2025, 6, 2)),
        (date(2025, 10, 1), date(2025, 10, 8)),
    ),
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
    """交易日数据采集窗口: 09:15-11:45 / 13:00-16:00, 仅工作日。

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
