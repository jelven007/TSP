"""Public extension projections shared by HTTP readers and background alerts."""
from __future__ import annotations

import glob
import logging
import math
import os
from datetime import date, datetime
from typing import Any

import polars as pl

from app.db_safe import is_valid_ext_ident, quote_ident
from app.services.ext_data import ExtConfigStore

logger = logging.getLogger(__name__)
_ext_value_map_cache: dict[tuple[str, str], tuple[Any, dict[str, Any]]] = {}


def parse_ext_columns(ext_columns: str) -> list[tuple[str, str]]:
    result = []
    for part in ext_columns.split(","):
        part = part.strip()
        if "." not in part:
            continue
        config_id, field_name = (piece.strip() for piece in part.split(".", 1))
        if not config_id or not field_name:
            continue
        if not is_valid_ext_ident(config_id) or "\x00" in field_name:
            continue
        result.append((config_id, field_name))
    return result


def parquet_glob(config, data_dir) -> str:
    cfg_dir = data_dir / "ext_data" / config.id
    if config.mode == "snapshot":
        return str(cfg_dir / "*.parquet")
    return str(cfg_dir / "timeseries" / "**" / "*.parquet")


def read_latest_ext_dataframe(config, data_dir) -> tuple[pl.DataFrame, str | None]:
    """Read the current snapshot or latest daily partition, preserving row order."""
    cfg_dir = data_dir / "ext_data" / config.id
    if config.mode == "snapshot":
        path = cfg_dir / "part.parquet"
        return (pl.read_parquet(path), None) if path.exists() else (pl.DataFrame(), None)
    base = cfg_dir / "timeseries"
    if not base.exists():
        return pl.DataFrame(), None
    partitions = sorted(
        d for d in base.iterdir()
        if d.is_dir() and d.name.startswith("date=") and (d / "part.parquet").exists()
    )
    if not partitions:
        return pl.DataFrame(), None
    latest = partitions[-1]
    return pl.read_parquet(latest / "part.parquet"), latest.name[5:]


def _ext_parquet_signature(config, data_dir):
    try:
        files = sorted(glob.glob(parquet_glob(config, data_dir), recursive=True))
        return tuple((f, os.path.getmtime(f)) for f in files) if files else None
    except Exception:
        return None


def _safe_ext_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def load_ext_value_maps(repo, ext_columns: str | None) -> dict[str, dict[str, Any]]:
    """Project extension columns without mutating shared strategy result files."""
    ext_specs = parse_ext_columns(ext_columns) if ext_columns else []
    if not ext_specs:
        return {}
    db = repo.store.db
    data_dir = repo.store.data_dir
    configs = {c.id: c for c in ExtConfigStore(data_dir).load_all()}
    value_maps: dict[str, dict[str, Any]] = {}
    for config_id, field_name in ext_specs:
        out_col = f"{config_id}__{field_name}"
        cfg = configs.get(config_id)
        cache_key = (config_id, field_name)
        sig = _ext_parquet_signature(cfg, data_dir) if cfg else None
        try:
            if cfg:
                cached = _ext_value_map_cache.get(cache_key)
                if cached is not None and sig is not None and cached[0] == sig:
                    value_maps[out_col] = cached[1]
                    continue
                ext_df, _ = read_latest_ext_dataframe(cfg, data_dir)
            else:
                view_name = f"ext_{config_id}"
                ext_df = pl.from_arrow(db.query(
                    f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                ).arrow())
            if ext_df.is_empty() or "symbol" not in ext_df.columns or field_name not in ext_df.columns:
                continue
            ext_df = ext_df.select(["symbol", field_name]).unique(subset=["symbol"], keep="last")
            vmap = {
                str(row["symbol"]): _safe_ext_value(row.get(field_name))
                for row in ext_df.to_dicts() if row.get("symbol")
            }
            value_maps[out_col] = vmap
            if cfg and sig is not None:
                _ext_value_map_cache[cache_key] = (sig, vmap)
        except Exception as exc:
            logger.debug("ext column join skipped for %s.%s: %s", config_id, field_name, exc)
    return value_maps
