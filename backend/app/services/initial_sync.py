"""First-install stock data initialization through the tracked pipeline."""
from __future__ import annotations

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from app.data_providers.registry import get_provider
from app.services import preferences
from app.services.fs_utils import atomic_write_parquet, atomic_write_text


def needs_initial_sync(data_dir: Path) -> bool:
    marker = data_dir / "initial_sync.json"
    if marker.exists():
        # A partial run must resume even after daily data has been written.
        try:
            return json.loads(marker.read_text(encoding="utf-8")).get("status") != "completed"
        except (ValueError, AttributeError):
            return True
    return not any((data_dir / "kline_daily").rglob("*.parquet"))


def mark_pending(data_dir: Path) -> None:
    atomic_write_text(data_dir / "initial_sync.json", json.dumps({"status": "pending"}))


def prepare_initial_sync(data_dir: Path) -> None:
    """Record a fresh install before live collectors can create daily files."""
    if needs_initial_sync(data_dir):
        mark_pending(data_dir)


async def start_initial_sync(state) -> str | None:
    """Enqueue once per process; failures remain retryable through /pipeline/run."""
    if getattr(state, "initial_sync_job_id", None):
        return None
    if not needs_initial_sync(state.repo.store.data_dir):
        return None
    from app.api.pipeline import trigger_pipeline_job

    response = await trigger_pipeline_job(
        state.repo,
        state.capabilities,
        getattr(state, "quote_service", None),
        source="startup",
    )
    if response["reused"]:
        return None
    state.initial_sync_job_id = response["job_id"]
    return response["job_id"]


def check_provider_routes() -> None:
    from app.services.provider_audit import audit_provider_routes

    audit = audit_provider_routes()
    if audit["unavailable"]:
        raise RuntimeError(
            "首次全量同步需要可用的数据源: " + ", ".join(audit["unavailable"])
        )


def sync_current_snapshots(data_dir: Path, symbols: list[str], *, write_lock=None) -> dict[str, int]:
    """Persist received quotes/books separately from official historical OHLC."""
    quotes_provider = get_provider(preferences.get_realtime_data_provider())
    records = (
        quotes_provider.get_realtime(symbols=symbols)
        if preferences.get_realtime_data_provider() == "tickflow"
        else quotes_provider.get_realtime()
    )
    quotes = records if isinstance(records, pl.DataFrame) else pl.DataFrame(records or [])
    if quotes.is_empty() or "symbol" not in quotes.columns:
        raise RuntimeError("首次实时行情同步返回空数据")
    quotes = quotes.filter(pl.col("symbol").is_in(symbols)).unique(subset=["symbol"])
    depth_provider = get_provider(preferences.get_depth5_data_provider())
    books = depth_provider.get_depth_batch(symbols)
    expected = set(symbols)
    depth = pl.DataFrame([
        {**record, "symbol": symbol}
        for symbol, record in (books or {}).items()
        if symbol in expected
    ])
    for label, frame in (("实时行情", quotes), ("五档盘口", depth)):
        covered = set(frame["symbol"].to_list()) if "symbol" in frame.columns else set()
        missing = expected - covered
        if missing:
            raise RuntimeError(f"首次{label}同步缺少 {len(missing)} 只股票,请重试")
    directory = data_dir / "market_snapshot"
    directory.mkdir(parents=True, exist_ok=True)
    received_at = datetime.now(UTC).isoformat()
    with write_lock if write_lock is not None else nullcontext():
        for filename, frame, source in (
            ("quotes.parquet", quotes, preferences.get_realtime_data_provider()),
            ("depth.parquet", depth, preferences.get_depth5_data_provider()),
        ):
            atomic_write_parquet(
                frame.with_columns(
                    pl.lit(received_at).alias("snapshot_received_at"),
                    pl.lit(source).alias("snapshot_source"),
                ),
                directory / filename,
            )
    return {"quote_rows": quotes.height, "depth_rows": depth.height}


def run_initial_sync(repo, capset, on_progress=None) -> dict:
    """Only successful initialization writes the completion marker."""
    from app.jobs import daily_pipeline

    mark_pending(repo.store.data_dir)
    check_provider_routes()
    result = daily_pipeline.run_now(repo, capset, on_progress=on_progress, initial_sync=True)
    atomic_write_text(repo.store.data_dir / "initial_sync.json", json.dumps({
        "status": "completed",
        "completed_at": datetime.now(UTC).isoformat(),
        "universe_size": result["universe_size"],
        "data_release_id": result["data_release_id"],
    }))
    return result
