"""固定起点轮询节奏: fetch 耗时吸收进 interval, 不叠加漂移。"""
from __future__ import annotations

from app.services.quote_service import _POLL_SLEEP_SLICE_S, _poll_sleep_slice


def test_waits_remaining_time_in_sliced_chunks() -> None:
    # 本轮 t=100 起, interval=1 → 截止 101。t=100.0 时还差 1s, 分片上限 0.5。
    assert _poll_sleep_slice(101.0, 100.0) == _POLL_SLEEP_SLICE_S
    # 临近截止: 返回精确剩余, 不超过剩余。
    assert _poll_sleep_slice(101.0, 100.8) == min(_POLL_SLEEP_SLICE_S, 101.0 - 100.8)


def test_fires_immediately_when_fetch_overran_the_interval() -> None:
    # fetch 耗时超过 interval → 截止已过, 立即进入下一轮 (None), 不补偿赤字。
    assert _poll_sleep_slice(101.0, 101.0) is None
    assert _poll_sleep_slice(101.0, 103.5) is None


def test_cadence_absorbs_fetch_cost_instead_of_adding_it() -> None:
    # 旧实现 cycle = fetch + interval; 新实现以 cycle_started 为基准。
    # 本轮 09:00:00 起, fetch 耗 0.4s (now=0.4), interval=1 → 截止 1.0, 还需 0.6s,
    # 整轮周期 = 1.0s 而非 1.4s。
    cycle_started = 0.0
    interval = 1.0
    now_after_fetch = 0.4
    next_cycle = cycle_started + interval
    assert _poll_sleep_slice(next_cycle, now_after_fetch) == min(_POLL_SLEEP_SLICE_S, 0.6)
