"""Immutable manifests for globally published local-data releases."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from app import __version__
from app.enriched_generation import (
    EnrichedGenerationUnavailableError,
    enriched_publication_incomplete,
    get_enriched_generation,
    guard_enriched_generations,
)
from app.services.provider_audit import audit_provider_routes

SCHEMA_VERSION = 1
_RELEASE_ROOT = "data_releases"

_DATASET_PATHS = {
    "stock_daily": "kline_daily",
    "stock_enriched": "kline_daily_enriched",
    "stock_minute": "kline_minute",
    "second_quotes": "quote_second",
    "stock_adj_factor": "adj_factor",
    "instruments": "instruments",
    "instrument_status": "instrument_status",
    "financials": "financials",
    "auction_snapshot": "auction_snapshot",
    "index_daily": "kline_index_daily",
    "index_enriched": "kline_index_enriched",
    "etf_daily": "kline_etf_daily",
    "etf_enriched": "kline_etf_enriched",
    "etf_minute": "kline_etf_minute",
    "etf_adj_factor": "adj_factor_etf",
    "depth5": "depth5",
    "market_snapshot": "market_snapshot",
}


class DataReleaseError(RuntimeError):
    """A release manifest is missing, malformed, or cannot be published."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _partition_date(path: Path) -> str | None:
    name = path.name
    if not name.startswith("date="):
        return None
    value = name.removeprefix("date=")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _dataset_snapshot(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {
            "state": "absent",
            "latest_partition": None,
            "partition_count": 0,
            "modified_at_ns": None,
        }

    partitions = [
        (value, child)
        for child in root.glob("date=*")
        if (value := _partition_date(child)) is not None
    ]
    latest_partition = max((value for value, _ in partitions), default=None)
    candidates = [root]
    candidates.extend(child for _, child in partitions)
    candidates.extend(child for child in root.iterdir() if child.is_file())
    candidates.extend(child for child in root.iterdir() if child.is_dir() and not partitions)
    modified_at_ns: int | None = None
    for candidate in candidates:
        try:
            value = candidate.stat().st_mtime_ns
        except OSError:
            continue
        modified_at_ns = value if modified_at_ns is None else max(modified_at_ns, value)

    has_content = bool(partitions) or any(root.iterdir())
    return {
        "state": "available" if has_content else "empty",
        "latest_partition": latest_partition,
        "partition_count": len(partitions),
        "modified_at_ns": modified_at_ns,
    }


def _generation(data_dir: Path, asset_type: str) -> str | None:
    try:
        return get_enriched_generation(data_dir, asset_type, initialize=False)
    except EnrichedGenerationUnavailableError:
        return None


def collect_dataset_snapshot(data_dir: Path) -> dict[str, dict[str, Any]]:
    """Collect bounded metadata without hashing or loading Parquet contents."""
    data_dir = Path(data_dir)
    datasets = {
        name: _dataset_snapshot(data_dir / relative)
        for name, relative in _DATASET_PATHS.items()
    }
    datasets["stock_enriched"]["generation"] = _generation(data_dir, "stock")
    datasets["etf_enriched"]["generation"] = _generation(data_dir, "etf")
    return datasets


def research_input_fingerprints(data_dir: Path) -> dict[str, Any]:
    """Hash compact research inputs; detect replacement while reading each file."""
    from app.market_time import trading_calendar_provenance

    paths = [
        "instruments/instruments.parquet",
        "instrument_status/history.parquet",
        *(f"financials/{table}/part.parquet"
          for table in ("metrics", "income", "balance_sheet", "cash_flow", "shares")),
    ]
    files = {}
    for relative in paths:
        path = Path(data_dir) / relative
        if not path.exists():
            files[relative] = None
            continue
        before = path.stat()
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat()
        if (before.st_ino, before.st_mtime_ns, before.st_size) != (
            after.st_ino, after.st_mtime_ns, after.st_size,
        ):
            raise DataReleaseError(f"research input changed while hashing: {relative}")
        files[relative] = {"sha256": digest, "bytes": after.st_size}
    return {"files": files, "trading_calendar": trading_calendar_provenance()}


def publish_data_release(
    data_dir: Path, *, reason: str,
    write_lock: AbstractContextManager[Any] | None = None,
) -> dict[str, Any]:
    """Serialize bounded release metadata/commit with repository file writers."""
    data_dir = Path(data_dir)
    # Capability detection may import providers; do it before blocking file writes.
    provider_audit = audit_provider_routes()
    with write_lock if write_lock is not None else nullcontext():
        return _publish_data_release(data_dir, reason=reason, provider_audit=provider_audit)


def _publish_data_release(
    data_dir: Path, *, reason: str, provider_audit: dict[str, Any],
) -> dict[str, Any]:
    generations = _publication_generations(data_dir)
    release_id = uuid.uuid4().hex
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "created_at": _utc_now(),
        "reason": str(reason),
        "app_version": __version__,
        "providers": {
            capability: route["provider"]
            for capability, route in provider_audit["routes"].items()
        },
        "provider_audit": provider_audit,
        "datasets": collect_dataset_snapshot(data_dir),
        "research_inputs": research_input_fingerprints(data_dir),
    }
    after = _publication_generations(data_dir)
    captured = {
        asset: manifest["datasets"][f"{asset}_enriched"]["generation"]
        for asset in generations
    }
    if generations != after or captured != after:
        raise DataReleaseError("enriched generation changed while collecting release metadata")
    _validate_manifest(manifest)
    release_root = data_dir / _RELEASE_ROOT
    history_path = release_root / "manifests" / f"{release_id}.json"
    current_path = release_root / "current.json"
    try:
        with guard_enriched_generations(data_dir, after):
            if research_input_fingerprints(data_dir) != manifest["research_inputs"]:
                raise DataReleaseError("research inputs changed before publishing release")
            _atomic_write_json(history_path, manifest)
            _atomic_write_json(current_path, manifest)
    except EnrichedGenerationUnavailableError as exc:
        raise DataReleaseError(str(exc)) from exc
    return manifest


def _publication_generations(data_dir: Path) -> dict[str, str | None]:
    generations = {}
    for asset in ("stock", "etf"):
        if enriched_publication_incomplete(data_dir, asset):
            raise DataReleaseError(f"{asset} enriched publication is incomplete")
        generations[asset] = _generation(data_dir, asset)
        if generations[asset] is None and enriched_publication_incomplete(data_dir, asset):
            raise DataReleaseError(f"{asset} enriched publication is incomplete")
    return generations


def _validate_manifest(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise DataReleaseError("data release manifest must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise DataReleaseError("unsupported data release manifest schema")
    release_id = payload.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise DataReleaseError("data release manifest has no release_id")
    if not isinstance(payload.get("datasets"), dict):
        raise DataReleaseError("data release manifest has no datasets")
    for name, dataset in payload["datasets"].items():
        if not isinstance(dataset, dict):
            raise DataReleaseError(f"invalid dataset metadata: {name}")
        if dataset.get("state") not in ("absent", "empty", "available"):
            raise DataReleaseError(f"invalid dataset state: {name}")
        for field in ("partition_count", "modified_at_ns"):
            value = dataset.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise DataReleaseError(f"invalid {field}: {name}")
        latest = dataset.get("latest_partition")
        if latest is not None and (
            not isinstance(latest, str) or _partition_date(Path(f"date={latest}")) != latest
        ):
            raise DataReleaseError(f"invalid latest_partition: {name}")
        generation = dataset.get("generation")
        if generation is not None and (not isinstance(generation, str) or not generation):
            raise DataReleaseError(f"invalid generation: {name}")
    providers = payload.get("providers", {})
    if not isinstance(providers, dict) or any(
        not isinstance(value, str) for value in providers.values()
    ):
        raise DataReleaseError("invalid release providers")
    return payload


def read_current_release(data_dir: Path) -> dict[str, Any] | None:
    path = Path(data_dir) / _RELEASE_ROOT / "current.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataReleaseError("data release manifest is unreadable") from exc
    return _validate_manifest(payload)


def ensure_current_release(data_dir: Path) -> dict[str, Any]:
    current = read_current_release(data_dir)
    return current if current is not None else publish_data_release(
        data_dir,
        reason="startup_baseline",
    )


def stable_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def persist_backtest_manifest(
    data_dir: Path,
    *,
    kind: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist compact trace metadata without duplicating large backtest output."""
    run_id = result.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise DataReleaseError("backtest result has no run_id")
    config = result.get("config")
    provenance = result.get("provenance")
    if not isinstance(config, Mapping) or not isinstance(provenance, Mapping):
        raise DataReleaseError("backtest result has no traceable config/provenance")
    stats = result.get("stats")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "kind": str(kind),
        "recorded_at": _utc_now(),
        "config": dict(config),
        "provenance": dict(provenance),
        "error": result.get("error"),
        "stats_hash": stable_hash(stats) if isinstance(stats, Mapping) else None,
        "trade_count": len(result.get("trades") or []),
    }
    path = Path(data_dir) / "backtest_results" / "manifests" / f"{run_id}.json"
    _atomic_write_json(path, manifest)
    return manifest


def _financial_revision_provenance(data_dir: Path) -> dict[str, Any]:
    """Bind a backtest to the newest retained financial observation per table.

    Consumes the observation store written at each financial sync so a run is
    reproducible against the exact restatement it saw. Observation time, not the
    report/announcement date, keys the store; a missing table stays ``None``
    before its first retained observation. Failures degrade to an error string
    rather than blocking the run.
    """
    from datetime import UTC, datetime

    from app.services.financial_sync import (
        FINANCIAL_TABLES,
        latest_financial_revision_meta,
    )

    now = datetime.now(UTC)
    tables: dict[str, Any] = {}
    try:
        for table in FINANCIAL_TABLES:
            tables[table] = latest_financial_revision_meta(data_dir, table, now)
    except (OSError, ValueError, KeyError) as exc:
        return {"captured_at": _utc_now(), "error": str(exc), "tables": {}}
    return {"captured_at": _utc_now(), "error": None, "tables": tables}


@lru_cache(maxsize=64)
def _parquet_rows(path: str, signature: tuple) -> int:
    import pyarrow.parquet as pq

    return pq.ParquetFile(path).metadata.num_rows


def historical_input_quality(data_dir: Path) -> dict[str, Any]:
    """Counts describe retained input rows, not an unknowable missing universe."""
    from app.services.financial_sync import FINANCIAL_TABLES

    tables, rows, errors = {}, 0, []
    for table in FINANCIAL_TABLES:
        count = 0
        try:
            for path in (data_dir / "financials" / table).glob("*.parquet"):
                stat = path.stat()
                count += _parquet_rows(str(path), (stat.st_ino, stat.st_mtime_ns, stat.st_size))
        except (OSError, ValueError) as exc:
            errors.append(f"{table}: {type(exc).__name__}")
            tables[table] = {"rows": None, "unknown_publication_ratio": None}
            continue
        rows += count
        # Current providers retain observations, not certified original disclosures.
        tables[table] = {"rows": count, "unknown_publication_ratio": 1.0 if count else None}
    return {
        "universe": {
            "survivorship_verified": False,
            "missing_delisted_ratio": None,
            "reason": "historical_universe_denominator_unavailable",
        },
        "financial_publication": {
            "basis": "retained_input_rows", "rows": None if errors else rows,
            "unknown_rows": None if errors else rows,
            "unknown_ratio": 1.0 if rows and not errors else None, "tables": tables, "errors": errors,
            "publication_time_verified": False,
        },
    }


@lru_cache(maxsize=8)
def _retained_input_metadata(path: str, signature: tuple) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if Path(path).name == ".research_snapshot.json":
        from app.services.research_snapshot import snapshot_summary

        return {"mode": "local_snapshot", **snapshot_summary(payload)}
    return {"mode": "public_release", "id": payload["id"], "file_count": len(payload["files"])}


def retained_input_provenance(data_dir: Path) -> dict | None:
    for name in (".research_snapshot.json", ".public_release.json"):
        path = data_dir / name
        if path.is_file():
            stat = path.stat()
            return _retained_input_metadata(
                str(path), (stat.st_ino, stat.st_mtime_ns, stat.st_size),
            ).copy()
    return None


def backtest_provenance(
    data_dir: Path,
    *,
    asset_type: str,
    config: Mapping[str, Any],
    strategy_hash: str | None = None,
    data_generation: str | None = None,
) -> dict[str, Any]:
    """Capture the release and managed generation used by a backtest."""
    data_dir = Path(data_dir)
    try:
        release = read_current_release(data_dir)
        release_error = None
    except DataReleaseError as exc:
        release = None
        release_error = str(exc)
    try:
        research_inputs = research_input_fingerprints(data_dir)
        research_inputs_error = None
    except (OSError, DataReleaseError) as exc:
        research_inputs = None
        research_inputs_error = str(exc)

    financial_revisions = _financial_revision_provenance(data_dir)

    if data_generation is None:
        data_generation = _generation(data_dir, asset_type)
    dataset_name = "etf_enriched" if asset_type == "etf" else "stock_enriched"
    release_dataset = (
        (release.get("datasets") or {}).get(dataset_name, {})
        if release is not None
        else {}
    )
    release_generation = release_dataset.get("generation")
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": _utc_now(),
        "dataset_release_id": release.get("release_id") if release else None,
        "dataset_release_created_at": release.get("created_at") if release else None,
        "dataset_release_reason": release.get("reason") if release else None,
        "asset_type": asset_type,
        "data_generation": data_generation,
        "release_generation": release_generation,
        "release_consistent": bool(
            release is not None
            and data_generation is not None
            and release_generation == data_generation
        ),
        "providers": dict(release.get("providers") or {}) if release else {},
        "config_hash": stable_hash(config),
        "strategy_hash": strategy_hash,
        "release_error": release_error,
        "research_inputs": research_inputs,
        "research_inputs_error": research_inputs_error,
        "research_inputs_match_release": bool(
            research_inputs is not None
            and release is not None
            and release.get("research_inputs") == research_inputs
        ),
        "financial_revisions": financial_revisions,
        "historical_inputs": historical_input_quality(data_dir),
        "input_snapshot": retained_input_provenance(data_dir),
    }


def release_health(data_dir: Path) -> dict[str, Any]:
    """Return current release readability and enriched-generation consistency."""
    try:
        release = read_current_release(data_dir)
    except DataReleaseError as exc:
        return {"status": "error", "release_id": None, "message": str(exc)}
    if release is None:
        try:
            _publication_generations(Path(data_dir))
        except DataReleaseError as exc:
            return {"status": "error", "release_id": None, "message": str(exc)}
        return {
            "status": "warning",
            "release_id": None,
            "message": "尚未发布数据 release manifest",
        }

    datasets = release.get("datasets") or {}
    mismatches: list[str] = []
    unavailable: list[str] = []
    for asset_type, dataset_name in (
        ("stock", "stock_enriched"),
        ("etf", "etf_enriched"),
    ):
        current = _generation(Path(data_dir), asset_type)
        if enriched_publication_incomplete(Path(data_dir), asset_type):
            unavailable.append(dataset_name)
        published = (datasets.get(dataset_name) or {}).get("generation")
        if current != published:
            mismatches.append(dataset_name)
    return {
        "status": "error" if unavailable else ("warning" if mismatches else "ok"),
        "release_id": release["release_id"],
        "created_at": release.get("created_at"),
        "reason": release.get("reason"),
        "generation_mismatches": mismatches,
        "unavailable_generations": unavailable,
        "message": (
            "enriched 发布未完成, 需要完整重建恢复"
            if unavailable
            else "存在尚未纳入全局 release 的 enriched 更新"
            if mismatches
            else None
        ),
    }
