"""实时行情写盘门闸 — 只在真正落盘/删除分区时短暂暂停轮询。

背景: 盘后管道与数据修正原先用 `with quote_service.paused():` 包住整段
run_now, 一次卡在取数阶段的修正任务曾把全市场实时行情冻结 55 分钟。
取数/计算阶段不写 parquet, 无需暂停; 只有落盘与删除分区期间才需要与
行情线程互斥。

门闸安装在线程本地: 修正任务在自己的执行线程内安装, 行情线程读不到,
因此行情自身的写入不会被自己暂停; 任务结束后自动清除。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_local = threading.local()


@contextmanager
def paused_quotes(quote_service) -> Iterator[None]:
    """在当前线程安装"写盘暂停实时行情"钩子, 退出时恢复上一层设置。

    quote_service 为 None 或没有 paused() 时退化为空操作。
    """
    guard = getattr(quote_service, "paused", None) if quote_service is not None else None
    previous = getattr(_local, "guard", None)
    _local.guard = guard if callable(guard) else None
    try:
        yield
    finally:
        _local.guard = previous


@contextmanager
def pause_quotes_for_write() -> Iterator[None]:
    """包住一次真实写盘/删分区; 未安装门闸时为空操作。"""
    guard = getattr(_local, "guard", None)
    if guard is None:
        yield
        return
    with guard():
        yield
