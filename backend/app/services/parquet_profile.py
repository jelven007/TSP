"""Exact incremental Parquet profiles, keyed by replacement-sensitive file identity."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.public_query_cache import public_query_checkpoint

logger = logging.getLogger(__name__)
PROFILE = ".query_profile.json"
DATASETS = ("kline_daily", "kline_daily_enriched", "kline_minute",
            "kline_5minute", "kline_10minute", "kline_15minute", "kline_weekly")
_MAX_BYTES = 16 * 1024 * 1024


def _inventory(root):
    result = {}

    def walk(directory, prefix=""):
        public_query_checkpoint()
        with os.scandir(directory) as entries:
            for entry in entries:
                public_query_checkpoint()
                name = prefix + entry.name
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path, name + "/")
                elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".parquet"):
                    stat = entry.stat(follow_symlinks=False)
                    result[name] = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]

    if root.exists():
        walk(root)
    return result


def _load(root):
    try:
        path = root / PROFILE
        if path.stat().st_size > _MAX_BYTES:
            return None
        profile = json.loads(path.read_text())
        symbols, files = profile["symbols"], profile["files"]
        if (profile["schema"] != 1 or not isinstance(symbols, list)
                or len(symbols) > 100_000 or len(set(symbols)) != len(symbols)
                or any(s is not None and not isinstance(s, str) for s in symbols)
                or not isinstance(files, dict) or len(files) > 100_000):
            return None
        for name, record in files.items():
            if (not isinstance(name, str) or not isinstance(record, dict)
                    or not isinstance(record.get("signature"), list)
                    or len(record["signature"]) != 4
                    or any(type(n) is not int or n < 0 for n in record["signature"])
                    or type(record.get("rows")) is not int or record["rows"] < 0
                    or not isinstance(record.get("mask"), str)
                    or len(record["mask"]) > (len(symbols) + 3) // 4 + 3):
                return None
            mask = int(record["mask"], 16)
            if mask < 0 or mask.bit_length() > len(symbols) or mask.bit_count() > record["rows"]:
                return None
        return profile
    except (OSError, ValueError, KeyError, TypeError):
        return None


def row_symbol_stats(root: Path) -> tuple[int, int]:
    """Read changed symbol columns only; never reuse a profile across replacement."""
    import polars as pl

    inventory = _inventory(root)
    previous = _load(root) or {"symbols": [], "files": {}}
    symbols = list(previous["symbols"])
    indices = {symbol: index for index, symbol in enumerate(symbols)}
    records, rows, union = {}, 0, 0
    for name, signature in inventory.items():
        public_query_checkpoint()
        record = previous["files"].get(name)
        if record is None or record["signature"] != signature:
            stats = pl.scan_parquet(root / name, hive_partitioning=False).select(
                pl.len().alias("rows"), pl.col("symbol").unique().implode().alias("symbols"),
            ).collect().row(0)
            mask = 0
            for symbol in stats[1]:
                if symbol not in indices:
                    indices[symbol] = len(symbols)
                    symbols.append(symbol)
                mask |= 1 << indices[symbol]
            record = {"signature": signature, "rows": int(stats[0]), "mask": hex(mask)}
        records[name] = record
        rows += record["rows"]
        union |= int(record["mask"], 16)
    public_query_checkpoint()
    profile = {"schema": 1, "symbols": symbols, "files": records}
    if profile != previous:
        if _inventory(root) != inventory:
            raise ValueError("Parquet changed while profiling; retry current data")
        text = json.dumps(profile, ensure_ascii=False, separators=(",", ":"))
        if len(text.encode()) <= _MAX_BYTES:
            try:
                atomic_write_text(root / PROFILE, text)
            except OSError:
                # Read-only legacy releases can still return freshly checked statistics.
                logger.debug("Parquet profile is not writable: %s", root)
    return rows, union.bit_count()


def prepare_profiles(root: Path) -> None:
    """Publisher prepares profiles before inventory; request TTL is not a rescan."""
    for name in DATASETS:
        directory = root / name
        if directory.exists():
            row_symbol_stats(directory)


def rebind_profiles(stage: Path) -> None:
    """Copies were checked against source inventory; bind counts to copied inodes."""
    for name in DATASETS:
        root = stage / name
        profile = _load(root)
        if profile is None:
            continue
        inventory = _inventory(root)
        if set(inventory) != set(profile["files"]):
            raise ValueError("Published Parquet profile inventory differs")
        for file, signature in inventory.items():
            record = profile["files"][file]
            if record["signature"][:2] != signature[:2]:
                raise ValueError("Published Parquet profile content differs")
            record["signature"] = signature
        atomic_write_text(root / PROFILE, json.dumps(profile, ensure_ascii=False))
