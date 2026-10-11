"""Bounded, causal second-snapshot replay with explicit research coverage."""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow.parquet as pq

from app.market_time import CN_TZ
from app.polars_guard import guarded_collect
from app.services.second_quote_archive import SCHEMA_VERSION, SNAPSHOT_SCHEMA


class SecondReplayDataError(RuntimeError):
    """Published input is corrupt, changed, duplicated, or out of order."""


@dataclass(frozen=True)
class ReplaySnapshot:
    capture_id: int
    collected_at_ms: int
    frame: pl.DataFrame


@dataclass(frozen=True)
class _Shard:
    path: Path
    size: int
    mtime_ns: int
    sha256: str
    first_ms: int
    last_ms: int
    first_capture: int
    last_capture: int


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def session_windows(day: date) -> list[tuple[int, int]]:
    """Continuous-auction research window; close auction is deliberately excluded."""
    return [
        (
            int(datetime.combine(day, start, CN_TZ).timestamp() * 1000),
            int(datetime.combine(day, end, CN_TZ).timestamp() * 1000),
        )
        for start, end in ((time(9, 30), time(11, 30)), (time(13), time(14, 57)))
    ]


class SecondQuoteReplay:
    def __init__(self, data_dir: Path) -> None:
        self._root = Path(data_dir) / "quote_second"
        self._frozen: tuple[date, date, tuple[_Shard, ...]] | None = None

    def available_dates(self) -> list[date]:
        result = []
        for partition in self._root.glob("date=*"):
            try:
                day = date.fromisoformat(partition.name.removeprefix("date="))
            except ValueError:
                continue
            if any(partition.glob("hour=*/*.parquet")):
                result.append(day)
        return sorted(result)

    def _files(self, start: date, end: date) -> tuple[_Shard, ...]:
        if end < start or (end - start).days > 30:
            raise ValueError("second replay range must contain 1-31 calendar days")
        if self._frozen is not None:
            if self._frozen[:2] != (start, end):
                raise ValueError("one replay instance must use one fixed date range")
            return self._frozen[2]
        files = []
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            for path in (self._root / f"date={day}").glob("hour=*/*.parquet"):
                try:
                    before = path.stat()
                    meta = pq.read_metadata(path)
                    if not meta.num_rows:
                        continue
                    column = meta.schema.names.index("collected_at_ms")
                    stats = [meta.row_group(i).column(column).statistics for i in range(meta.num_row_groups)]
                    if any(item is None or not item.has_min_max for item in stats):
                        raise ValueError("missing collection-time statistics")
                    capture_column = meta.schema.names.index("capture_id")
                    capture_stats = [meta.row_group(i).column(capture_column).statistics for i in range(meta.num_row_groups)]
                    if any(item is None or not item.has_min_max for item in capture_stats):
                        raise ValueError("missing capture-id statistics")
                    digest = _digest(path)
                    after = path.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("shard changed while freezing")
                    files.append(_Shard(
                        path, after.st_size, after.st_mtime_ns, digest,
                        min(item.min for item in stats), max(item.max for item in stats),
                        min(item.min for item in capture_stats), max(item.max for item in capture_stats),
                    ))
                except Exception as exc:
                    raise SecondReplayDataError(f"invalid second quote shard {path.name}: {exc}") from exc
        files.sort(key=lambda item: (item.first_ms, item.path.name))
        for left, right in pairwise(files):
            if right.first_ms <= left.last_ms:
                raise SecondReplayDataError("overlapping second quote shards; duplicate or concurrent writers")
            if right.first_capture <= left.last_capture:
                raise SecondReplayDataError("capture_id repeated or regressed across shards")
        self._frozen = (start, end, tuple(files))
        return tuple(files)

    def manifest(self, start: date, end: date) -> dict:
        parts = [
            {
                "path": shard.path.relative_to(self._root).as_posix(),
                "size": shard.size, "sha256": shard.sha256,
            }
            for shard in self._files(start, end)
        ]
        encoded = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()
        return {"schema_version": SCHEMA_VERSION, "files": parts, "fingerprint": hashlib.sha256(encoded).hexdigest()}

    def _read(self, shard: _Shard, symbols: list[str] | None, asset_type: str | None) -> pl.DataFrame:
        try:
            stat = shard.path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (shard.size, shard.mtime_ns) or _digest(shard.path) != shard.sha256:
                raise ValueError("frozen shard changed")
            lf = pl.scan_parquet(shard.path, hive_partitioning=False)
            schema = lf.collect_schema()
            missing = set(SNAPSHOT_SCHEMA) - set(schema.names())
            if missing:
                raise ValueError(f"missing columns: {sorted(missing)}")
            invalid_types = [key for key, dtype in SNAPSHOT_SCHEMA.items() if schema[key] != dtype]
            if invalid_types:
                raise ValueError(f"invalid schema types: {invalid_types}")
            # Validate ordering and schema on the full shard, including unselected symbols.
            clocks = guarded_collect(lf.select(
                "capture_id", "collected_at_ms", "schema_version", "trade_date", "asset_type", "symbol",
            ))
            if any(clocks.null_count().row(0)):
                raise ValueError("null capture clock")
            if clocks["schema_version"].unique().to_list() != [SCHEMA_VERSION]:
                raise ValueError("unsupported schema version")
            if not clocks["collected_at_ms"].is_sorted() or not clocks["capture_id"].is_sorted():
                raise ValueError("collection clock regressed")
            if clocks.select(pl.struct("capture_id", "asset_type", "symbol").is_duplicated().any()).item():
                raise ValueError("duplicate security within capture")
            if clocks.group_by("capture_id").agg(pl.col("collected_at_ms").n_unique())["collected_at_ms"].max() != 1:
                raise ValueError("one capture has inconsistent timestamps")
            day = date.fromisoformat(shard.path.parent.parent.name.removeprefix("date="))
            lo = int(datetime.combine(day, time.min, CN_TZ).timestamp() * 1000)
            if clocks["trade_date"].unique().to_list() != [day] or clocks["collected_at_ms"].min() < lo or clocks["collected_at_ms"].max() >= lo + 86_400_000:
                raise ValueError("partition date and capture clock disagree")
            if symbols:
                lf = lf.filter(pl.col("symbol").is_in(symbols))
            if asset_type:
                lf = lf.filter(pl.col("asset_type") == asset_type)
            frame = guarded_collect(lf)
            after = shard.path.stat()
            if (after.st_size, after.st_mtime_ns) != (shard.size, shard.mtime_ns):
                raise ValueError("shard changed while reading")
            return frame
        except Exception as exc:
            raise SecondReplayDataError(f"cannot replay {shard.path.name}: {exc}") from exc

    def iter_snapshots(
        self, start: date, end: date, *, symbols: list[str] | None = None,
        asset_type: str | None = None, start_ms: int | None = None, end_ms: int | None = None,
    ) -> Iterator[ReplaySnapshot]:
        seen: set[int] = set()
        for shard in self._files(start, end):
            if start_ms is not None and shard.last_ms < start_ms:
                continue
            if end_ms is not None and shard.first_ms > end_ms:
                continue
            frame = self._read(shard, symbols, asset_type)
            if start_ms is not None:
                frame = frame.filter(pl.col("collected_at_ms") >= start_ms)
            if end_ms is not None:
                frame = frame.filter(pl.col("collected_at_ms") <= end_ms)
            for snapshot in frame.partition_by("capture_id", maintain_order=True):
                capture_id = int(snapshot["capture_id"][0])
                if capture_id in seen:
                    raise SecondReplayDataError("capture_id repeated across shards")
                seen.add(capture_id)
                yield ReplaySnapshot(capture_id, int(snapshot["collected_at_ms"][0]), snapshot)

    def sample(self, start: date, end: date, *, symbols: list[str], limit: int = 10_000) -> pl.DataFrame:
        if not symbols or any(not symbol.strip() for symbol in symbols):
            raise ValueError("symbols must not be empty")
        remaining = max(1, min(100_001, int(limit)))
        frames = []
        for snapshot in self.iter_snapshots(start, end, symbols=symbols):
            frames.append(snapshot.frame.head(remaining))
            remaining -= frames[-1].height
            if remaining <= 0:
                break
        return pl.concat(frames) if frames else pl.DataFrame(schema=SNAPSHOT_SCHEMA)

    def validate(
        self, start: date, end: date, *, expected_interval_s: float = 1.0,
        expected_symbols: int | None = None, min_coverage_ratio: float = 1.0,
        symbols: list[str] | None = None, asset_type: str = "stock",
        start_ms: int | None = None, end_ms: int | None = None,
        trading_dates: list[date] | None = None, max_source_age_ms: int = 5_000,
    ) -> dict[str, Any]:
        if not math.isfinite(expected_interval_s) or not 0 < expected_interval_s <= 60:
            raise ValueError("expected_interval_s must be in (0, 60]")
        if not 0 < min_coverage_ratio <= 1 or (expected_symbols is not None and expected_symbols < 1):
            raise ValueError("invalid coverage requirement")
        if symbols is not None and (not symbols or any(not symbol.strip() for symbol in symbols)):
            raise ValueError("symbols must not be empty")
        if not 0 <= max_source_age_ms <= 60_000:
            raise ValueError("invalid source freshness limit")
        if (start_ms is None) != (end_ms is None) or (start_ms is not None and start_ms >= end_ms):
            raise ValueError("both window endpoints are required and end must follow start")
        manifest = self.manifest(start, end)
        required = set(symbols or ())
        days = sorted(set(trading_dates if trading_dates is not None else ([start] if start == end else [])))
        if any(day < start or day > end for day in days):
            raise ValueError("trading_dates outside range")
        scoped = bool(required and days and start_ms is not None)
        windows = [
            (day, max(lo, start_ms or lo), min(hi, end_ms or hi))
            for day in days for lo, hi in session_windows(day)
            if max(lo, start_ms or lo) < min(hi, end_ms or hi)
        ]
        if start_ms is not None:
            first = datetime.fromtimestamp(start_ms / 1000, CN_TZ).date()
            last = datetime.fromtimestamp(end_ms / 1000, CN_TZ).date()
            if first < start or last > end or not windows:
                raise ValueError("window must intersect declared continuous trading sessions")
        rows = captures = source_rows = stale_rows = invalid_rows = inferred_rows = incomplete = 0
        timestamps: list[int] = []
        observed_days: set[date] = set()
        providers: set[str] = set()
        versions: set[str] = set()
        tracked = ["symbol", "trade_date", "source_ts_ms", "volume", "amount"]
        previous = pl.DataFrame(schema={key: SNAPSHOT_SCHEMA[key] for key in tracked})
        regressions = resets = 0
        counts = []
        for snap in self.iter_snapshots(start, end, symbols=symbols, asset_type=asset_type, start_ms=start_ms, end_ms=end_ms):
            stamp = snap.collected_at_ms
            day = datetime.fromtimestamp(stamp / 1000, CN_TZ).date()
            if trading_dates is not None and day not in days:
                continue
            if not any(lo <= stamp < hi for lo, hi in session_windows(day)):
                continue
            frame = snap.frame
            rows += frame.height
            captures += 1
            timestamps.append(stamp)
            observed_days.add(day)
            present = set(frame["symbol"].to_list())
            counts.append(len(present))
            expected = len(required) if required else expected_symbols
            if expected and len(present) / expected < min_coverage_ratio:
                incomplete += 1
            source_rows += frame["source_ts_ms"].is_not_null().sum()
            inferred_rows += frame["source_date_inferred"].sum()
            providers.update(value or "unknown" for value in frame["provider"].unique())
            versions.update(value or "unknown" for value in frame["provider_version"].unique())
            checked = frame.join(previous, on="symbol", how="left", suffix="_prior")
            same_day = pl.col("trade_date") == pl.col("trade_date_prior")
            metrics = checked.select(
                (~(pl.lit(stamp) - pl.col("source_ts_ms")).is_between(0, max_source_age_ms))
                .fill_null(True).sum().alias("stale"),
                pl.any_horizontal([
                    ~pl.col(key).is_finite() | (pl.col(key) < (0.000001 if key == "last_price" else 0))
                    for key in ("last_price", "volume", "amount")
                ]).fill_null(True).sum().alias("invalid"),
                (same_day & (pl.col("source_ts_ms") < pl.col("source_ts_ms_prior")))
                .fill_null(False).sum().alias("regressions"),
                (same_day & ((pl.col("volume") < pl.col("volume_prior")) | (pl.col("amount") < pl.col("amount_prior"))))
                .fill_null(False).sum().alias("resets"),
            ).row(0, named=True)
            stale_rows += metrics["stale"]
            invalid_rows += metrics["invalid"]
            regressions += metrics["regressions"]
            resets += metrics["resets"]
            previous = pl.concat([
                frame.select(tracked),
                previous.join(frame.select("symbol"), on="symbol", how="anti"),
            ])
        if not required and expected_symbols is None and counts:
            expected_symbols = max(counts)
            incomplete = sum(count / expected_symbols < min_coverage_ratio for count in counts)
        threshold = expected_interval_s * 1.5
        gaps = []
        for left, right in pairwise(timestamps):
            day = datetime.fromtimestamp(left / 1000, CN_TZ).date()
            if any(lo <= left <= right < hi for lo, hi in session_windows(day)):
                gaps.append((right - left) / 1000)
        boundary_gaps = []
        for _, lo, hi in windows:
            found = [stamp for stamp in timestamps if lo <= stamp <= hi]
            boundary_gaps.extend(
                [(found[0] - lo) / 1000, (hi - found[-1]) / 1000] if found else [(hi - lo) / 1000]
            )
        gap_count = sum(gap > threshold for gap in gaps + boundary_gaps)
        missing_days = [day.isoformat() for day in days if day not in observed_days]
        issues = []
        for condition, message in (
            (not scoped, "window_or_expected_universe_unspecified"),
            (bool(missing_days), "missing_trading_dates"),
            (bool(gap_count), "collection_gaps"),
            (bool(incomplete), "incomplete_captures"),
            (bool(stale_rows), "missing_stale_or_future_source_time"),
            (bool(invalid_rows), "invalid_price_or_cumulative_values"),
            (bool(regressions or resets), "source_or_cumulative_values_regressed"),
            (len(providers) > 1 or len(versions) > 1, "mixed_provider_versions"),
            ("unknown" in providers or "unknown" in versions, "unknown_provider_version"),
        ):
            if condition:
                issues.append(message)
        return {
            "status": "missing" if not rows else "ready" if not issues else "warning",
            "scope": "requested_window" if scoped else "observed_samples",
            "files": len(manifest["files"]), "fingerprint": manifest["fingerprint"],
            "rows": rows, "captures": captures, "gap_count": gap_count,
            "max_gap_s": round(max(gaps + boundary_gaps, default=0.0), 3),
            "gap_threshold_s": threshold, "expected_symbols": len(required) or expected_symbols,
            "observed_max_symbols": max(counts, default=0), "incomplete_captures": incomplete,
            "source_time_coverage": source_rows / rows if rows else None,
            "inferred_source_date_rows": inferred_rows, "stale_source_rows": stale_rows,
            "invalid_rows": invalid_rows, "source_regressions": regressions,
            "cumulative_resets": resets, "missing_dates": missing_days,
            "first_collected_at_ms": min(timestamps, default=None),
            "last_collected_at_ms": max(timestamps, default=None),
            "providers": sorted(providers), "provider_versions": sorted(versions),
            "issues": issues, "exchange_event_time_verified": False,
        }
