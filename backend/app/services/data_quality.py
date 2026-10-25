"""Bounded quality evidence; availability never certifies historical PIT."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.services.data_release import historical_input_quality, release_health
from app.services.provider_audit import audit_provider_routes

_DATASETS = {
    "daily": ("kline_daily", "daily"),
    "minute": ("kline_minute", "minute"),
    "enriched": ("kline_daily_enriched", None),
    "financials": ("financials", "financial"),
    "instruments": ("instruments", None),
}
MAX_PARTITION_FILES = 64
MAX_PARTITION_BYTES = 32 * 1024**2


def latest_partition_quality(root: Path, expected_symbols: int | None) -> dict:
    """Inspect only the newest daily partition, within an explicit byte limit."""
    result = {
        "partition": None, "rows": None, "symbols": None,
        "expected_symbols": expected_symbols, "coverage_ratio": None,
        "invalid_price_rows": None, "invalid_volume_rows": None,
        "basis": "latest_partition_current_catalog", "error": None,
    }
    try:
        partitions = []
        for path in root.glob("date=*"):
            try:
                day = date.fromisoformat(path.name.removeprefix("date="))
            except ValueError:
                continue
            if path.is_dir():
                partitions.append((day, path))
        if not partitions:
            return result
        day, partition = max(partitions)
        result["partition"] = day.isoformat()
        files = sorted(partition.glob("*.parquet"))
        if not files:
            result["error"] = "empty_partition"
            return result
        if (len(files) > MAX_PARTITION_FILES
                or sum(path.stat().st_size for path in files) > MAX_PARTITION_BYTES):
            result["error"] = "partition_inspection_limit"
            return result
        frame = pl.scan_parquet(files, hive_partitioning=False, extra_columns="ignore")
        columns = set(frame.collect_schema().names())
        prices = {"open", "high", "low", "close"}
        metrics = [pl.len().alias("rows"), pl.col("symbol").n_unique().alias("symbols")]
        if prices <= columns:
            invalid = pl.any_horizontal([
                ~pl.col(name).is_finite() | (pl.col(name) <= 0) | pl.col(name).is_null()
                for name in sorted(prices)
            ]) | (pl.col("high") < pl.max_horizontal("open", "close", "low")) | (
                pl.col("low") > pl.min_horizontal("open", "close", "high")
            )
            metrics.append(invalid.sum().alias("invalid_price_rows"))
        if "volume" in columns:
            metrics.append((
                ~pl.col("volume").is_finite() | (pl.col("volume") < 0)
                | pl.col("volume").is_null()
            ).sum().alias("invalid_volume_rows"))
        result.update(frame.select(metrics).collect().row(0, named=True))
        if expected_symbols and result["symbols"] is not None:
            result["coverage_ratio"] = result["symbols"] / expected_symbols
    except (OSError, ValueError, pl.exceptions.PolarsError) as exc:
        result["error"] = type(exc).__name__
    return result


def quality_summary(data_dir: Path, stats: dict) -> dict:
    """Reuse already cached table counts; do not scan full historical payloads."""
    routes = audit_provider_routes()["routes"]
    datasets = {}
    for name, (_, capability) in _DATASETS.items():
        table = stats.get(name)
        datasets[name] = {
            "state": "available" if table else "unknown_or_empty",
            "rows": table.get("rows") if table else None,
            "earliest_date": table.get("earliest_date") if table else None,
            "latest_date": table.get("latest_date", table.get("latest_as_of")) if table else None,
            "source": routes.get(capability, {}).get("provider") if capability else None,
            "source_clock_verified": None,
        }
    catalog = stats.get("instruments") or {}
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "datasets": datasets,
        "latest_daily": latest_partition_quality(
            data_dir / "kline_daily", catalog.get("symbols_covered"),
        ),
        "historical_inputs": historical_input_quality(data_dir),
        "release": release_health(data_dir),
        "historical_pit_verified": False,
    }


def initialization_status(data_dir: Path, stats: dict, jobs: list[dict], *, auto_enabled: bool,
                          indicators_ready: bool) -> dict:
    """Report persisted completion, active execution and actual prerequisites."""
    marker, marker_error = {}, None
    try:
        path = data_dir / "initial_sync.json"
        if path.stat().st_size > 16 * 1024:
            raise ValueError("initialization marker exceeds limit")
        marker = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(marker, dict) or marker.get("status") not in {"pending", "completed"}:
            raise ValueError("invalid initialization marker")
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        marker_error = type(exc).__name__
    latest = next((job for job in jobs if job.get("kind") == "daily_pipeline"), None)
    active = latest and latest.get("status") in {"queued", "running", "cancelling"}
    overall = (
        latest["status"] if active else "completed" if marker.get("status") == "completed"
        else "retry_required" if latest and latest.get("status") in {
            "failed", "cancelled", "interrupted",
        } else "manual" if not auto_enabled else "pending"
    )
    routes = audit_provider_routes()["routes"]
    stage_datasets = {
        "sync_instruments": "instruments", "sync_daily": "daily", "sync_minute": "minute",
        "compute_enriched": "enriched", "sync_financial": "financials",
    }
    current_dataset = stage_datasets.get(latest.get("stage")) if active else None
    stages = {}
    for name, (_, capability) in _DATASETS.items():
        usable = routes.get(capability, {}).get("usable") if capability else True
        stages[name] = (
            "running" if current_dataset == name
            else "unavailable" if usable is False
            else "warming" if name == "enriched" and not indicators_ready
            else "available" if stats.get(name)
            else "waiting"
        )
    # Dataset completion cannot certify independent model/delivery configuration.
    stages.update(ai="unknown", notifications="unknown")
    return {
        "auto_enabled": auto_enabled, "status": overall, "marker_error": marker_error,
        "completed_at": marker.get("completed_at"), "data_release_id": marker.get("data_release_id"),
        "job_id": latest.get("id") if latest else None,
        "progress": latest.get("progress") if active else None,
        "stages": stages,
        "last_successful_pipeline_at": next((
            job.get("finished_at") for job in jobs
            if job.get("kind") == "daily_pipeline" and job.get("status") == "succeeded"
        ), None),
    }
