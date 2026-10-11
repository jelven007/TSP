"""行情时钟契约: 本地接收时间和无日期单票事件时间不能确认收盘。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.market_time import CN_TZ


def source_snapshot_ms(record: dict[str, Any]) -> int | None:
    """读取可信源端快照时间; 旧插件未声明 basis 时保留 timestamp 契约。"""
    basis = record.get("timestamp_basis", "source_snapshot")
    if basis == "source_event":
        return None
    value = record.get("source_timestamp")
    if value is None:
        if basis != "source_snapshot":
            return None
        value = record.get("timestamp")
    try:
        stamp = int(value)
        datetime.fromtimestamp(stamp / 1000, tz=CN_TZ)
        return stamp if stamp > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def confirms_boundary(
    record: dict[str, Any], boundary_ms: int, *, slack_ms: int = 0,
) -> bool:
    stamp = source_snapshot_ms(record)
    if stamp is None:
        return False
    boundary_day = datetime.fromtimestamp(boundary_ms / 1000, tz=CN_TZ).date()
    return (
        datetime.fromtimestamp(stamp / 1000, tz=CN_TZ).date() == boundary_day
        and stamp >= boundary_ms - slack_ms
    )
