"""Official dated suspension events, independent of price and listing history."""
from datetime import date
from pathlib import Path

import polars as pl

RELATIVE_PATH = Path("instrument_status/suspensions.parquet")
SCHEMA = {"symbol": pl.String, "effective_date": pl.Date, "announced_on": pl.Date,
          "is_suspended": pl.Boolean, "source_url": pl.String}
DATE_COLUMN = "_suspension_date"
STATUS_COLUMN = "_suspension_status"


def load_suspension_history(data_dir: Path) -> pl.DataFrame:
    path = Path(data_dir) / RELATIVE_PATH
    if not path.exists():
        return pl.DataFrame(schema=SCHEMA)
    try:
        frame = pl.read_parquet(path).select(*SCHEMA).cast(SCHEMA)
        if any(frame[name].null_count() for name in SCHEMA):
            raise ValueError("null evidence")
        if frame.filter(
            (pl.col("symbol").str.strip_chars() == "")
            | ~pl.col("source_url").str.starts_with("https://")
        ).height:
            raise ValueError("missing evidence source")
        if frame.unique(["symbol", "effective_date", "announced_on"]).height != frame.height:
            raise ValueError("duplicate events")
        return frame.sort(["symbol", "effective_date", "announced_on"])
    except Exception as exc:
        raise ValueError("停复牌历史证据无效或不可读") from exc


def dated_status(history: pl.DataFrame, day: date) -> pl.DataFrame:
    """Same-day publications have no known intraday time; consume from tomorrow."""
    if history.is_empty():
        return pl.DataFrame(schema={"symbol": pl.String, DATE_COLUMN: pl.Date, STATUS_COLUMN: pl.Boolean})
    return (
        history.filter((pl.col("effective_date") <= day) & (pl.col("announced_on") < day))
        .sort(["symbol", "effective_date", "announced_on"])
        .unique("symbol", keep="last", maintain_order=True)
        .select("symbol", pl.lit(day).alias(DATE_COLUMN), pl.col("is_suspended").alias(STATUS_COLUMN))
    )


def pool_evidence(repo, day: date, universe: pl.DataFrame | None = None) -> pl.DataFrame | None:
    """Attach status to an existing directory, or return status-only evidence."""
    getter = getattr(repo, "get_suspension_history", None)
    if not callable(getter):
        return universe
    status = dated_status(getter(), day)
    if status.is_empty():
        return universe
    if universe is None or universe.is_empty():
        return status
    return universe.join(status, on="symbol", how="left")
