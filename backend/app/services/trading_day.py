"""交易日探针 (oracle) — 回答「今天是否 A 股交易日」。

优先使用版本化的交易所休市安排。未覆盖年份按当前实时源探测, 不跨源:
完整覆盖目标日期的 provider 日历可以判定休市; 当日指数 K 或可信快照可证明
交易。旧行情只能说明源端未更新, 不能证明休市, 因此返回 None。
盘中采集可在未知时保留诊断; 自动盘后定版必须取得 True。本模块不落盘。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.market_time import CN_TZ, as_cn_datetime, cn_now, published_trading_day

# 一篮流动性票: 探 max(timestamp), 任一戳为今日即交易日 (OR 语义)。
# 大盘蓝筹同日全部停牌 = 市场性事件, 与休市同处理无碍。
_BASKET = ("000001.SZ", "600519.SH", "600036.SH", "601318.SH", "000651.SZ")

_TTL_TRADING_S = 3600.0   # 交易日结论每小时复探 (跨日天然失效)
_TTL_HOLIDAY_S = 1800.0   # 休市结论 30 分钟复探, 误判自愈上限
_TTL_UNKNOWN_S = 300.0    # 未知结论 5 分钟后重试探测

_CACHE_LOCK = threading.Lock()


@dataclass
class _Cache:
    day: object | None = None
    provider: str | None = None
    verdict: bool | None = None
    probed_at: float = 0.0


_CACHE = _Cache()


def reset_cache() -> None:
    """清空探针缓存 (测试用)。"""
    with _CACHE_LOCK:
        _CACHE.day = None
        _CACHE.provider = None
        _CACHE.verdict = None
        _CACHE.probed_at = 0.0


def _probe_fuyao(now: datetime) -> bool | None:
    """Probe the selected custom source without crossing provider boundaries.

    The historical name is retained for compatibility with existing callers
    and tests; it now supports any selected custom provider.
    """
    try:
        from app.data_providers import custom as custom_sources
        from app.services import preferences

        provider_name = preferences.get_realtime_data_provider()
        if provider_name == "tickflow" or not custom_sources.is_custom_provider(provider_name):
            return None
        provider = custom_sources.get_provider(provider_name)
        trading_days = getattr(provider, "trading_days", None)
        if callable(trading_days):
            days = trading_days()
            if days and now.date() in days:
                return True
            # 历史日期集合不能推断最后一日之后的未来休市安排。
            if days and min(days) <= now.date() <= max(days):
                return False
            return None

        if not custom_sources.provider_has_dataset(provider_name, "daily"):
            return None
        frame = provider.get_daily(
            ["000001.SH"],
            now - timedelta(days=10),
            now,
            asset_type="index",
        )
        if frame is None or frame.is_empty() or "date" not in frame.columns:
            return None
        latest = frame["date"].max()
        if latest == now.date():
            return True
        return None
    except Exception:  # noqa: BLE001 — 探针失败按未知处理, 不上抛
        return None


def _probe_tickflow(now: datetime) -> bool | None:
    """tickflow 行情时间戳: max(timestamp) 日期 == 今天 ⇔ 交易日。

    戳停在上一交易日: None (可能是源端延迟或缓存, 不能反证休市)。
    无实时权限 / 网络失败 / 无有效戳 → None。
    """
    try:
        from app.services import preferences
        from app.tickflow.client import get_client

        if preferences.get_realtime_data_provider() != "tickflow":
            return None
        rows = get_client().quotes.get(symbols=list(_BASKET)) or []
        stamps = [r.get("timestamp") for r in rows if isinstance(r, dict)]
        valid = [int(t) for t in stamps if isinstance(t, (int, float)) and t]
        if not valid:
            return None
        latest_day = datetime.fromtimestamp(max(valid) / 1000, tz=CN_TZ).date()
        if latest_day == now.date():
            return True
        return None
    except Exception:  # noqa: BLE001 — 无权限/网络失败按未知处理
        return None


def is_trading_day(now: datetime | None = None) -> bool | None:
    """今天是否 A 股交易日。True=交易日, False=确定休市, None=未知 (维持周几近似)。

    公布的休市表零请求判断; 覆盖外使用当前源证据, 按日期、源及 TTL 缓存。
    """
    from app.services import preferences

    now = as_cn_datetime(now or cn_now())
    published = published_trading_day(now.date())
    if published is not None:
        return published
    provider_name = preferences.get_realtime_data_provider()

    with _CACHE_LOCK:
        # 「未知」(None) 也是一个结论, 同样按 TTL 缓存 —— 它正是 _TTL_UNKNOWN_S 要
        # 挡住的场景 (未配 fuyao 且 tickflow 不可用时, 轮询每拍都会重打一次探测)。
        # _CACHE.day 只在探测写回时设置, 因此「当天已探过」用它判定即可。
        if (
            _CACHE.day == now.date()
            and _CACHE.provider == provider_name
            and (time.monotonic() - _CACHE.probed_at) < _ttl_of(_CACHE.verdict)
        ):
            return _CACHE.verdict

    verdict = _probe_fuyao(now)
    if verdict is None:
        verdict = _probe_tickflow(now)

    with _CACHE_LOCK:
        _CACHE.day = now.date()
        _CACHE.provider = provider_name
        _CACHE.verdict = verdict
        _CACHE.probed_at = time.monotonic()
    return verdict


def _ttl_of(verdict: bool | None) -> float:
    if verdict is True:
        return _TTL_TRADING_S
    if verdict is False:
        return _TTL_HOLIDAY_S
    return _TTL_UNKNOWN_S
