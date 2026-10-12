"""Append-only second-level quote snapshot archive for causal replay."""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import CN_TZ, cn_today
from app.services.fs_utils import atomic_write_text
from app.tickflow.repository import replace_with_retry

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_FLUSH_ROUNDS = 10
DEFAULT_QUEUE_CAPACITY = 30
_FLUSH_INTERVAL_S = 10.0
_SOURCE_TIME_RE = re.compile(
    r"^(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d{1,6}))?$"
)

SNAPSHOT_SCHEMA = {
    "schema_version": pl.UInt16,
    "trade_date": pl.Date,
    "capture_id": pl.Int64,
    "collected_at_ms": pl.Int64,
    "provider_ts_ms": pl.Int64,
    "source_ts_ms": pl.Int64,
    "source_time": pl.String,
    "source_date_inferred": pl.Boolean,
    "provider": pl.String,
    "provider_version": pl.String,
    "asset_type": pl.String,
    "symbol": pl.String,
    "last_price": pl.Float64,
    "prev_close": pl.Float64,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "change_pct": pl.Float64,
    "change_amount": pl.Float64,
    "amplitude": pl.Float64,
    "turnover_rate": pl.Float64,
    "session": pl.String,
    "bid1": pl.Float64,
    "ask1": pl.Float64,
    "bid1_volume": pl.Float64,
    "ask1_volume": pl.Float64,
}


def _integer(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: object) -> float | None:
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number is None or math.isfinite(number) else None


def source_time_to_timestamp_ms(source_time: object, trade_date: date) -> int | None:
    text = str(source_time or "").strip()
    match = _SOURCE_TIME_RE.fullmatch(text)
    if match is None:
        return None
    values = {name: int(match.group(name)) for name in ("hour", "minute", "second")}
    if values["hour"] > 23 or values["minute"] > 59 or values["second"] > 59:
        return None
    fraction = (match.group("fraction") or "").ljust(6, "0")
    value = datetime.combine(
        trade_date,
        dt_time(
            values["hour"],
            values["minute"],
            values["second"],
            int(fraction or 0),
        ),
        tzinfo=CN_TZ,
    )
    return int(value.timestamp() * 1000)


def build_snapshot_frame(
    records_by_asset: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    provider: str,
    capture_id: int,
    collected_at_ms: int,
    trade_date: date,
) -> pl.DataFrame:
    """Normalize one full-market capture without conflating its three clocks."""
    rows: list[dict[str, Any]] = []
    for asset_type in ("stock", "etf", "index"):
        for record in records_by_asset.get(asset_type, ()):
            symbol = str(record.get("symbol") or "").strip()
            if not symbol:
                continue
            source_time = str(record.get("source_time") or "").strip() or None
            explicit_source_ts = _integer(record.get("source_timestamp"))
            inferred_source_ts = (
                source_time_to_timestamp_ms(source_time, trade_date)
                if explicit_source_ts is None and source_time is not None
                else None
            )
            rows.append({
                "schema_version": SCHEMA_VERSION,
                "trade_date": trade_date,
                "capture_id": int(capture_id),
                "collected_at_ms": int(collected_at_ms),
                "provider_ts_ms": _integer(record.get("timestamp")),
                "source_ts_ms": (
                    explicit_source_ts if explicit_source_ts is not None else inferred_source_ts
                ),
                "source_time": source_time,
                "source_date_inferred": inferred_source_ts is not None,
                "provider": str(provider),
                "provider_version": str(record.get("provider_version") or "unknown"),
                "asset_type": asset_type,
                "symbol": symbol,
                "last_price": _number(record.get("last_price")),
                "prev_close": _number(record.get("prev_close")),
                "open": _number(record.get("open")),
                "high": _number(record.get("high")),
                "low": _number(record.get("low")),
                "volume": _number(record.get("volume")),
                "amount": _number(record.get("amount")),
                "change_pct": _number(record.get("change_pct")),
                "change_amount": _number(record.get("change_amount")),
                "amplitude": _number(record.get("amplitude")),
                "turnover_rate": _number(record.get("turnover_rate")),
                "session": (
                    str(record.get("session"))
                    if record.get("session") is not None
                    else None
                ),
                **{
                    key: _number(record.get(key))
                    for key in ("bid1", "ask1", "bid1_volume", "ask1_volume")
                },
            })
    return pl.DataFrame(rows, schema=SNAPSHOT_SCHEMA)


@dataclass(frozen=True)
class _ArchiveBatch:
    records_by_asset: Mapping[str, Sequence[Mapping[str, Any]]]
    provider: str
    capture_id: int
    collected_at_ms: int
    trade_date: date


class SecondQuoteArchive:
    """Bounded async writer for immutable second-quote Parquet shards."""

    def __init__(
        self,
        data_dir: Path,
        *,
        enabled: Callable[[], bool],
        retention_days: Callable[[], int],
        flush_rounds: int = DEFAULT_FLUSH_ROUNDS,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        min_free_bytes: int = 1024**3,
    ) -> None:
        self._root = Path(data_dir) / "quote_second"
        self._enabled = enabled
        self._retention_days = retention_days
        self._flush_rounds = max(1, int(flush_rounds))
        self._queue: queue.Queue[object] = queue.Queue(maxsize=max(1, int(queue_capacity)))
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._closed = False
        self._session_id = uuid.uuid4().hex
        self._min_free_bytes = min_free_bytes
        self._accepted_rounds = 0
        self._written_rounds = 0
        self._written_rows = 0
        self._written_files = 0
        self._dropped_rounds = 0
        self._last_flush_ms: int | None = None
        self._last_error: str | None = None
        self._last_prune_date: date | None = None
        self._pending_rounds = 0
        self._queue_peak = 0
        self._write_ms: float | None = None
        self._last_drop_ms: int | None = None

    def submit(
        self,
        records_by_asset: Mapping[str, Sequence[Mapping[str, Any]]],
        *,
        provider: str,
        capture_id: int | None = None,
        collected_at_ms: int | None = None,
        trade_date: date | None = None,
    ) -> bool:
        """Queue one capture without blocking the realtime quote thread."""
        try:
            if not self._enabled():
                return False
        except Exception as exc:
            self._set_error(f"archive preference unavailable: {exc}")
            return False
        if not any(records_by_asset.get(asset) for asset in ("stock", "etf", "index")):
            return False
        batch = _ArchiveBatch(
            # The caller may reuse dictionaries after this method returns.
            records_by_asset={
                asset: tuple(dict(record) for record in records_by_asset.get(asset, ()))
                for asset in ("stock", "etf", "index")
            },
            provider=str(provider),
            capture_id=int(time.time_ns() if capture_id is None else capture_id),
            collected_at_ms=int(
                time.time() * 1000 if collected_at_ms is None else collected_at_ms
            ),
            trade_date=trade_date or cn_today(),
        )
        # Serialize stop vs admission; there is never a batch behind a stop marker.
        with self._thread_lock:
            if self._closed:
                return False
            self._ensure_thread()
            try:
                self._queue.put_nowait(batch)
            except queue.Full:
                self._record_loss(1, "archive queue full; capture dropped")
                return False
            with self._state_lock:
                self._accepted_rounds += 1
                self._queue_peak = max(self._queue_peak, self._queue.qsize())
        return True

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._writer_loop, name="second-quote-archive", daemon=True,
        )
        self._thread.start()

    def reopen(self) -> None:
        """Allow a new lifecycle only after the previous writer drained."""
        with self._thread_lock:
            if self._closed and self._thread is not None and self._thread.is_alive():
                raise RuntimeError("second quote writer is still stopping")
            self._stop_event.clear()
            self._closed = False

    def _record_loss(self, rounds: int, message: str) -> None:
        with self._state_lock:
            self._dropped_rounds += rounds
            self._last_error = message
            self._last_drop_ms = int(time.time() * 1000)
        logger.error("秒级归档丢失 %d 轮: %s", rounds, message)

    def _writer_loop(self) -> None:
        pending: dict[tuple[date, str], list[pl.DataFrame]] = {}
        pending_rounds = 0
        last_flush = time.monotonic()
        try:
            self._persist_state(clean_shutdown=False)
            while not self._stop_event.is_set() or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=0.25)
                except queue.Empty:
                    item = None
                if item is not None:
                    try:
                        assert isinstance(item, _ArchiveBatch)
                        frame = build_snapshot_frame(
                            item.records_by_asset,
                            provider=item.provider,
                            capture_id=item.capture_id,
                            collected_at_ms=item.collected_at_ms,
                            trade_date=item.trade_date,
                        )
                        hour = datetime.fromtimestamp(item.collected_at_ms / 1000, CN_TZ)
                        if hour.date() != item.trade_date:
                            raise ValueError("capture date does not match collection clock")
                        pending.setdefault((item.trade_date, hour.strftime("%H")), []).append(frame)
                        pending_rounds += 1
                        with self._state_lock:
                            self._pending_rounds = pending_rounds
                    except Exception as exc:
                        self._record_loss(1, f"archive normalization failed: {exc}")
                    finally:
                        self._queue.task_done()
                if pending_rounds >= self._flush_rounds or time.monotonic() - last_flush >= _FLUSH_INTERVAL_S:
                    self._flush_pending(pending)
                    pending.clear()
                    pending_rounds = 0
                    with self._state_lock:
                        self._pending_rounds = 0
                    self._persist_state(clean_shutdown=False)
                    last_flush = time.monotonic()
            self._flush_pending(pending)
            with self._state_lock:
                self._pending_rounds = 0
            self._persist_state(clean_shutdown=True)
        except Exception as exc:
            self._set_error(f"archive writer stopped: {exc}")
            logger.exception("秒级行情归档线程异常退出")

    def _flush_pending(self, pending: Mapping[tuple[date, str], list[pl.DataFrame]]) -> None:
        try:
            self._prune_if_due()
        except OSError as exc:
            self._set_error(f"archive retention scan failed: {exc}")
        for (trade_date, hour), frames in pending.items():
            if not frames:
                continue
            temporary = None
            published = False
            started = time.perf_counter()
            try:
                frame = pl.concat(frames, how="vertical_relaxed").sort(
                    ["collected_at_ms", "capture_id", "asset_type", "symbol"]
                )
                directory = self._root / f"date={trade_date.isoformat()}" / f"hour={hour}"
                directory.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(directory).free < self._min_free_bytes:
                    raise OSError("insufficient disk space for second quote archive")
                first_ms = int(frame["collected_at_ms"].min())
                last_ms = int(frame["collected_at_ms"].max())
                out = directory / f"part-{first_ms:013d}-{last_ms:013d}-{uuid.uuid4().hex}.parquet"
                temporary = out.with_name(out.name + ".tmp")
                frame.write_parquet(
                    temporary, compression="zstd", compression_level=3, statistics=True,
                )
                # Windows 下 fsync 走 _commit, 只读句柄会 EBADF; 用读写句柄才是跨平台
                # 可靠地把数据刷盘后再原子替换。
                with temporary.open("rb+") as stream:
                    os.fsync(stream.fileno())
                replace_with_retry(temporary, out)
                published = True
                with self._state_lock:
                    self._written_rounds += len(frames)
                    self._written_rows += frame.height
                    self._written_files += 1
                    self._last_flush_ms = int(time.time() * 1000)
                    self._write_ms = round((time.perf_counter() - started) * 1000, 3)
                # Directory fsync is not supported on Windows. A post-publication
                # error must never cause another copy of this shard to be written.
                if os.name != "nt":
                    descriptor = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            except Exception as exc:
                if published:
                    self._set_error(f"archive directory sync failed: {exc}")
                else:
                    self._record_loss(len(frames), f"archive write failed: {exc}")
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        logger.warning("秒级归档临时文件清理失败")

    def _persist_state(self, *, clean_shutdown: bool) -> None:
        """Diagnostic only; Parquet contents remain the authoritative record."""
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            state = self.status()
            state.update(schema_version=SCHEMA_VERSION, clean_shutdown=clean_shutdown)
            atomic_write_text(
                self._root / f"session-{self._session_id}.json",
                json.dumps(state, ensure_ascii=False, sort_keys=True),
            )
        except Exception as exc:
            self._set_error(f"archive state persistence failed: {exc}")
            logger.warning("秒级归档状态写盘失败: %s", exc)

    def _prune_if_due(self) -> None:
        today = cn_today()
        if self._last_prune_date == today:
            return
        try:
            keep_days = max(0, min(365, int(self._retention_days())))
        except Exception as exc:
            self._set_error(f"archive retention preference unavailable: {exc}")
            return
        if keep_days == 0:
            return
        cutoff = today - timedelta(days=keep_days - 1)
        if self._root.exists():
            for partition in self._root.glob("date=*"):
                try:
                    partition_date = date.fromisoformat(
                        partition.name.removeprefix("date=")
                    )
                except ValueError:
                    continue
                if partition.is_dir() and not partition.is_symlink() and partition_date < cutoff:
                    try:
                        shutil.rmtree(partition)
                    except OSError as exc:
                        self._set_error(f"archive retention cleanup failed: {exc}")
        self._last_prune_date = today

    def _set_error(self, message: str) -> None:
        with self._state_lock:
            self._last_error = message

    def stop(self, timeout: float = 10.0) -> bool:
        """Drain accepted captures and publish the final shard before shutdown."""
        with self._thread_lock:
            self._closed = True
            self._stop_event.set()
            thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout=max(0.1, timeout))
        stopped = not thread.is_alive()
        if stopped:
            with self._thread_lock:
                if self._thread is thread:
                    self._thread = None
        else:
            self._set_error("archive writer did not stop before timeout")
        return stopped

    def status(self) -> dict[str, Any]:
        try:
            enabled = bool(self._enabled())
        except Exception:
            enabled = False
        try:
            retention_days = max(0, min(365, int(self._retention_days())))
        except Exception:
            retention_days = None
        with self._state_lock:
            state = {
                "accepted_rounds": self._accepted_rounds,
                "written_rounds": self._written_rounds,
                "written_rows": self._written_rows,
                "written_files": self._written_files,
                "dropped_rounds": self._dropped_rounds,
                "last_flush_ms": self._last_flush_ms,
                "last_error": self._last_error,
                "pending_rounds": self._pending_rounds,
                "queue_peak": self._queue_peak,
                "last_write_ms": self._write_ms,
                "last_drop_ms": self._last_drop_ms,
            }
        thread = self._thread
        return {
            "enabled": enabled,
            "running": thread is not None and thread.is_alive(),
            "retention_days": retention_days,
            "flush_rounds": self._flush_rounds,
            "queue_capacity": self._queue.maxsize,
            "queued_rounds": self._queue.qsize(),
            **state,
        }
