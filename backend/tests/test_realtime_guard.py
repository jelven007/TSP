"""实时行情写盘门闸 — 暂停窗口只覆盖真实写盘, 且按线程隔离。"""
from __future__ import annotations

import threading

from app.services import realtime_guard
from app.services.quote_service import QuoteService


def test_paused_quotes_is_noop_without_service():
    with realtime_guard.paused_quotes(None), realtime_guard.pause_quotes_for_write():
        pass


def test_paused_quotes_ignores_service_without_paused():
    class Stub:
        def is_paused(self) -> bool:
            return False

    with realtime_guard.paused_quotes(Stub()), realtime_guard.pause_quotes_for_write():
        pass


def test_write_gate_pauses_only_inside_write():
    qs = QuoteService()
    with realtime_guard.paused_quotes(qs):
        assert not qs.is_paused()
        with realtime_guard.pause_quotes_for_write():
            assert qs.is_paused()
        assert not qs.is_paused()
    # 门闸退出后不再影响写盘路径
    with realtime_guard.pause_quotes_for_write():
        assert not qs.is_paused()


def test_write_gate_is_thread_local():
    qs = QuoteService()
    seen: dict[str, bool] = {}

    with realtime_guard.paused_quotes(qs):
        def worker() -> None:
            with realtime_guard.pause_quotes_for_write():
                seen["paused"] = qs.is_paused()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    # 行情线程读不到修正任务安装的门闸, 自身写盘不会被自我暂停
    assert seen["paused"] is False


def test_pause_depth_counts_nested_scopes():
    qs = QuoteService()
    qs.pause()
    qs.pause()
    assert qs.is_paused()
    qs.resume()
    assert qs.is_paused()
    qs.resume()
    assert not qs.is_paused()
