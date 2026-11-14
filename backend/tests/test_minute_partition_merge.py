"""分钟分区合并回归 (issue #305): 单股补齐只合并触及 symbol, 未触及行原样保留。

旧实现把整个已存分区与新增行 concat 后全量 unique+sort: 单股补齐为合并 1 只
股票的几行数据, 需读出全市场单日分区做全量去重重写, 写放大导致全局写锁被
长时间持有, 看门狗误判进程僵死。新实现只对触及 symbol 子集去重, 未触及行
原样保留, 最终仍整体排序, 保持「按 symbol,datetime 全局有序」的文件不变量。
"""
from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest

from app.services.kline_sync import _write_minute_partition


def _bars(spec: dict[str, list[tuple[str, float]]]) -> pl.DataFrame:
    rows = [
        {"symbol": sym, "datetime": datetime.fromisoformat(dt), "close": px}
        for sym, bars in spec.items()
        for dt, px in bars
    ]
    return pl.DataFrame(rows)


def _read(out) -> pl.DataFrame:
    return pl.read_parquet(out).sort("symbol", "datetime")


def test_fresh_write_creates_sorted_partition(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    df = _bars({
        "000001.SZ": [("2026-01-05 09:31:00", 10.0), ("2026-01-05 09:32:00", 10.1)],
        "600000.SH": [("2026-01-05 09:31:00", 8.0)],
    })
    written = _write_minute_partition(df, minute_dir)

    assert written == 3
    out = minute_dir / "date=2026-01-05" / "part.parquet"
    assert out.exists()
    stored = _read(out)
    assert stored.height == 3
    assert stored["symbol"].to_list() == ["000001.SZ", "000001.SZ", "600000.SH"]


def test_merge_preserves_untouched_symbols_and_keeps_last_for_touched(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    first = _bars({
        "000001.SZ": [("2026-01-05 09:31:00", 10.0), ("2026-01-05 09:32:00", 10.1)],
        "600000.SH": [("2026-01-05 09:31:00", 8.0), ("2026-01-05 09:32:00", 8.1)],
        "300750.SZ": [("2026-01-05 09:31:00", 20.0)],
    })
    _write_minute_partition(first, minute_dir)
    out = minute_dir / "date=2026-01-05" / "part.parquet"

    # 单股补齐: 仅 000001.SZ, 含同 (symbol, datetime) 修正行与一根新增 K
    second = _bars({
        "000001.SZ": [("2026-01-05 09:32:00", 10.9), ("2026-01-05 09:33:00", 10.2)],
    })
    written = _write_minute_partition(second, minute_dir)

    stored = _read(out)
    # 未触及 symbol 原样保留
    assert stored.filter(pl.col("symbol") == "600000.SH")["close"].to_list() == [8.0, 8.1]
    assert stored.filter(pl.col("symbol") == "300750.SZ")["close"].to_list() == [20.0]
    # 触及 symbol: 同键取后到者, 新键保留, 无重复
    a = stored.filter(pl.col("symbol") == "000001.SZ")
    assert a["close"].to_list() == [10.0, 10.9, 10.2]
    assert stored.height == 6
    assert written == 6
    assert stored.select(["symbol", "datetime"]).unique().height == stored.height
    # 全局有序不变量
    assert stored.equals(stored.sort("symbol", "datetime"))


def test_multi_date_input_writes_one_partition_per_day(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    df = _bars({
        "000001.SZ": [("2026-01-05 09:31:00", 10.0), ("2026-01-06 09:31:00", 10.5)],
    })
    written = _write_minute_partition(df, minute_dir)

    assert written == 2
    assert (minute_dir / "date=2026-01-05" / "part.parquet").exists()
    assert (minute_dir / "date=2026-01-06" / "part.parquet").exists()
    assert _read(minute_dir / "date=2026-01-06" / "part.parquet").height == 1


def test_existing_null_datetime_rows_are_dropped_on_merge(tmp_path):
    minute_dir = tmp_path / "kline_minute"
    _write_minute_partition(
        _bars({"000001.SZ": [("2026-01-05 09:31:00", 10.0)]}), minute_dir
    )
    out = minute_dir / "date=2026-01-05" / "part.parquet"
    # 模拟历史脏数据: 追加一行 datetime 为 null 的记录
    dirty = pl.concat([
        pl.read_parquet(out),
        pl.DataFrame({"symbol": ["600000.SH"], "datetime": [None], "close": [9.9]}),
    ], how="diagonal")
    dirty.write_parquet(out)

    _write_minute_partition(
        _bars({"000001.SZ": [("2026-01-05 09:32:00", 10.1)]}), minute_dir
    )

    stored = _read(out)
    assert stored["datetime"].null_count() == 0
    assert stored.height == 2


def test_concurrent_writers_preserve_every_symbol(tmp_path, monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from app.services import kline_sync

    minute_dir = tmp_path / "kline_minute"
    _write_minute_partition(
        _bars({"600000.SH": [("2026-01-05 09:31:00", 8.0)]}), minute_dir,
    )
    write = kline_sync._atomic_write_parquet

    def slow_write(frame, path):
        time.sleep(0.02)
        write(frame, path)

    monkeypatch.setattr(kline_sync, "_atomic_write_parquet", slow_write)
    frames = [
        _bars({f"{index:06d}.SZ": [("2026-01-05 09:32:00", float(index))]})
        for index in range(1, 9)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda frame: _write_minute_partition(frame, minute_dir), frames))
    result = _read(minute_dir / "date=2026-01-05" / "part.parquet")
    assert result.height == 9
    assert result["symbol"].n_unique() == 9


def test_independent_process_writers_do_not_lose_updates(tmp_path):
    import subprocess
    import sys

    minute_dir = tmp_path / "kline_minute"
    code = """
import sys
from pathlib import Path
from datetime import datetime
import polars as pl
from app.services.kline_sync import _write_minute_partition
frame = pl.DataFrame({
    "symbol": [sys.argv[2]],
    "datetime": [datetime(2026, 1, 5, 9, 31)],
    "close": [10.0],
})
_write_minute_partition(frame, Path(sys.argv[1]))
"""
    processes = [
        subprocess.Popen([sys.executable, "-c", code, str(minute_dir), f"{index:06d}.SZ"])
        for index in range(1, 5)
    ]
    for process in processes:
        assert process.wait(timeout=30) == 0
    result = _read(minute_dir / "date=2026-01-05" / "part.parquet")
    assert result["symbol"].to_list() == [f"{index:06d}.SZ" for index in range(1, 5)]


def _halt_history(tmp_path, events):
    from app.instrument_suspension import SCHEMA, TIMESTAMP_SCHEMA

    out = tmp_path / "instrument_status" / "suspensions.parquet"
    out.parent.mkdir(exist_ok=True)
    pl.DataFrame(events, schema={**SCHEMA, **TIMESTAMP_SCHEMA}).write_parquet(out)


def _halt_event(*, suspended=True, announced_at=datetime(2026, 1, 4, 18)):
    return {
        "symbol": "000001.SZ", "effective_date": datetime(2026, 1, 5).date(),
        "announced_on": announced_at.date(), "announced_at": announced_at,
        "is_suspended": suspended, "source_url": "https://www.szse.cn/notice.pdf",
    }


@pytest.mark.parametrize("only_missing", [False, True])
def test_known_halt_placeholders_cannot_be_reintroduced(tmp_path, only_missing):
    _halt_history(tmp_path, [_halt_event()])
    frame = _bars({
        "000001.SZ": [("2026-01-05 09:31:00", 10.)],
        "600000.SH": [("2026-01-05 09:31:00", 8.)],
    }).with_columns(pl.lit(0.).alias("volume"), pl.lit(0.).alias("amount"))
    minute_dir = tmp_path / "kline_minute"
    _write_minute_partition(frame, minute_dir, only_missing=only_missing)
    stored = _read(minute_dir / "date=2026-01-05" / "part.parquet")
    assert stored["symbol"].to_list() == ["600000.SH"]
    _write_minute_partition(frame, minute_dir, only_missing=only_missing)
    assert _read(minute_dir / "date=2026-01-05" / "part.parquet").equals(stored)


def test_active_minutes_conflicting_with_known_halt_are_rejected(tmp_path):
    _halt_history(tmp_path, [_halt_event()])
    frame = _bars({"000001.SZ": [("2026-01-05 09:31:00", 10.)]}).with_columns(
        pl.lit(1.).alias("volume"), pl.lit(1000.).alias("amount"),
    )
    with pytest.raises(ValueError, match=r"停牌.*成交"):
        _write_minute_partition(frame, tmp_path / "kline_minute")
    assert not (tmp_path / "kline_minute" / "date=2026-01-05" / "part.parquet").exists()


@pytest.mark.parametrize("case", ["resume_before_open", "late_halt", "unknown_time"])
def test_halt_filter_uses_only_evidence_available_at_target_open(tmp_path, case):
    if case == "resume_before_open":
        events = [_halt_event(), _halt_event(
            suspended=False, announced_at=datetime(2026, 1, 5, 9, 15),
        )]
    else:
        event = _halt_event(announced_at=datetime(2026, 1, 5, 10))
        if case == "unknown_time":
            event["announced_at"] = None
        events = [event]
    _halt_history(tmp_path, events)
    frame = _bars({"000001.SZ": [("2026-01-05 09:31:00", 10.)]}).with_columns(
        pl.lit(0.).alias("volume"), pl.lit(0.).alias("amount"),
    )
    minute_dir = tmp_path / "kline_minute"
    _write_minute_partition(frame, minute_dir)
    assert _read(minute_dir / "date=2026-01-05" / "part.parquet").height == 1
