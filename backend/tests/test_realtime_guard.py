"""实时行情写盘门闸 — 暂停窗口只覆盖真实写盘, 且按线程隔离。"""
from __future__ import annotations

import threading

import polars as pl
import pytest

from app.services import realtime_guard
from app.services.quote_service import QuoteService
from app.tickflow.repository import KlineRepository


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


@pytest.mark.parametrize("failure", [False, True])
def test_repository_write_uses_write_lock_without_pausing_quotes(tmp_path, failure):
    qs = QuoteService()
    repo = KlineRepository.__new__(KlineRepository)
    repo._write_lock = threading.Lock()
    out = tmp_path / "part.parquet"
    previous = pl.DataFrame({"symbol": ["600000.SH"], "date": ["2026-10-21"], "close": [10.0]})
    incoming = previous.with_columns(pl.lit(11.0).alias("close"))
    previous.write_parquet(out)

    def write(frame, path):
        assert repo._write_lock.locked()
        assert not qs.is_paused()
        if failure:
            raise OSError("simulated write failure")
        KlineRepository._atomic_write_parquet(frame, path)

    repo._atomic_write_parquet = write
    with realtime_guard.paused_quotes(qs):
        if failure:
            with pytest.raises(OSError, match="simulated write failure"):
                repo._optimistic_upsert_partition(out, incoming, None)
        else:
            repo._optimistic_upsert_partition(out, incoming, None)
    assert not qs.is_paused()
    assert not repo._write_lock.locked()
    assert pl.read_parquet(out).equals(previous if failure else incoming)
