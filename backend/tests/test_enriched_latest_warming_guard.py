"""enriched 最新日热路径守卫测试 — 请求线程不得同步做全量重算。

缓存被扩展表写入/自定义信号变更失效后, tick 热路径 (自选 enriched 端点)
原实现会在每个请求线程里同步跑 300 天 scan + compute; 并发请求还会在
重任务槽位上排队并各自重复计算, 最终超过前端 30s 超时。
修复契约: 冷缓存只负责起后台预热, 读取侧 (refresh=False) 立即返回空表。
"""
from __future__ import annotations

import threading
import time

import polars as pl

from app.tickflow.repository import KlineRepository


def _bare_repo() -> KlineRepository:
    """跳过 __init__ (避免 DataStore/目录依赖), 只装配守卫涉及的属性。"""
    repo = KlineRepository.__new__(KlineRepository)
    repo._enriched_cache = None
    repo._enriched_cache_date = None
    repo._enriched_warming = False
    repo._etf_enriched_cache = None
    repo._index_enriched_cache = None
    repo._asset_enriched_warming = set()
    repo._asset_warmup_lock = threading.Lock()
    return repo


def test_get_enriched_latest_refresh_false_does_not_rebuild():
    repo = _bare_repo()
    calls: list[int] = []

    def _spy_refresh():
        calls.append(1)

    repo._refresh_enriched = _spy_refresh  # type: ignore[method-assign]

    df, cache_date = repo.get_enriched_latest(refresh=False)

    assert df.is_empty()
    assert cache_date is None
    assert calls == [], "refresh=False 时不得在请求线程同步重算"


def test_ensure_enriched_warmup_only_starts_when_cold():
    repo = _bare_repo()
    started: list[str] = []

    repo._start_enriched_warmup = lambda: started.append("stock")  # type: ignore[method-assign]

    repo.ensure_enriched_warmup()
    assert started == ["stock"]

    repo._enriched_cache = pl.DataFrame({"symbol": ["600000.SH"]})
    repo.ensure_enriched_warmup()
    assert started == ["stock"], "暖缓存不得重复预热"


def test_ensure_enriched_warmup_dispatches_only_requested_assets():
    repo = _bare_repo()
    started: list[str] = []

    repo._start_enriched_warmup = lambda: started.append("stock")  # type: ignore[method-assign]
    repo._start_asset_enriched_warmup = started.append  # type: ignore[method-assign]

    repo.ensure_enriched_warmup(("etf", "index"))

    assert started == ["etf", "index"]
    assert "stock" not in started


def test_asset_enriched_warmup_runs_single_flight():
    repo = _bare_repo()
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def _refresh():
        calls.append(1)
        started.set()
        release.wait(5)

    repo._refresh_etf_enriched = _refresh  # type: ignore[method-assign]

    repo.ensure_enriched_warmup(("etf",))
    assert started.wait(5), "预热线程未启动"
    repo.ensure_enriched_warmup(("etf",))
    assert repo._asset_enriched_warming == {"etf"}
    assert calls == [1], "预热进行中不得重复起线程"

    release.set()
    deadline = time.time() + 5
    while repo._asset_enriched_warming and time.time() < deadline:
        time.sleep(0.01)
    assert repo._asset_enriched_warming == set()
