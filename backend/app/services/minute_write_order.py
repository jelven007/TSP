"""Private request ordering for 1m upserts; versions commit with the data footer."""
from __future__ import annotations

import base64
import sys
import zlib
from array import array
from pathlib import Path

import polars as pl

VERSION_COLUMN = "_minute_request_seq"
METADATA_KEY = "tsp.minute.request_order.v1"
_COUNTER = ".minute-request-sequence"
_MAX_SEQUENCE = (1 << 64) - 1


def begin_minute_request(minute_dir: Path) -> int:
    """Allocate before fetching, shared by threads/processes and across restarts.

    The counter belongs to this data directory and must travel with its backups.
    Allocation failure aborts the request; never fall back to an unversioned write.
    """
    from app.services.kline_sync import _minute_partition_lock
    from app.tickflow.repository import replace_with_retry

    minute_dir.mkdir(parents=True, exist_ok=True)
    counter = minute_dir / _COUNTER
    with _minute_partition_lock(counter):
        previous = 0
        if counter.exists():
            text = counter.read_text(encoding="ascii")
            if not text.isdecimal():
                raise ValueError("invalid minute request sequence")
            previous = int(text)
        if not 0 <= previous < _MAX_SEQUENCE:
            raise ValueError("minute request sequence exhausted or invalid")
        sequence = previous + 1
        temporary = counter.with_name(counter.name + ".tmp")
        try:
            temporary.write_text(str(sequence), encoding="ascii")
            replace_with_retry(temporary, counter)
        finally:
            temporary.unlink(missing_ok=True)
        return sequence


def read_versions(out: Path, height: int) -> pl.Series | None:
    """Read while holding the partition lock; malformed metadata fails closed."""
    encoded = pl.read_parquet_metadata(out).get(METADATA_KEY)
    if encoded is None:
        return None
    try:
        compressed = base64.b64decode(encoded, validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, height * 8 + 1)
        if len(raw) != height * 8 or not decoder.eof or decoder.unused_data:
            raise ValueError("invalid minute version length")
        values = array("Q")
        values.frombytes(raw)
        if sys.byteorder != "little":
            values.byteswap()
        return pl.Series(VERSION_COLUMN, values, dtype=pl.UInt64)
    except (ValueError, zlib.error) as exc:
        raise ValueError("invalid minute request-order metadata") from exc


def version_metadata(frame: pl.DataFrame) -> dict[str, str]:
    values = array("Q", frame[VERSION_COLUMN])
    if sys.byteorder != "little":
        values.byteswap()
    return {METADATA_KEY: base64.b64encode(zlib.compress(values.tobytes())).decode("ascii")}


def accept_rows(
    incoming: pl.DataFrame, existing: pl.DataFrame | None,
    request_seq: int | None, *, only_missing: bool,
) -> pl.DataFrame:
    """A sequence identifies one response: repeats/conflicts cannot rewrite it."""
    if request_seq is not None and (
        type(request_seq) is not int or not 0 < request_seq <= _MAX_SEQUENCE
    ):
        raise ValueError("request_seq must be a positive UInt64")
    sequence = request_seq or 0
    incoming = incoming.with_columns(pl.lit(sequence, dtype=pl.UInt64).alias(VERSION_COLUMN))
    if existing is None or existing.is_empty():
        return incoming
    keys = ["symbol", "datetime"]
    prior_column = "_previous_minute_request_seq"
    prior = existing.select(*keys, pl.col(VERSION_COLUMN).alias(prior_column)).unique(
        keys, keep="last",
    )
    joined = incoming.join(prior, on=keys, how="left")
    accepted = pl.col(prior_column).is_null()
    if not only_missing:
        accepted |= pl.col(prior_column) < sequence
        # Unversioned legacy keys retain the old upsert contract.
        if request_seq is None:
            accepted |= pl.col(prior_column) == 0
    return joined.filter(accepted).drop(prior_column)
