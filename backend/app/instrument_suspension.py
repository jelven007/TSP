"""Official dated suspension events, independent of price and listing history."""
from datetime import date, datetime
from pathlib import Path

import polars as pl

RELATIVE_PATH = Path("instrument_status/suspensions.parquet")
SCHEMA = {"symbol": pl.String, "effective_date": pl.Date, "announced_on": pl.Date,
          "is_suspended": pl.Boolean, "source_url": pl.String}
DATE_COLUMN = "_suspension_date"
STATUS_COLUMN = "_suspension_status"
TIMESTAMP_SCHEMA = {"announced_at": pl.Datetime("us")}


def load_suspension_history(data_dir: Path) -> pl.DataFrame:
    path = Path(data_dir) / RELATIVE_PATH
    if not path.exists():
        return pl.DataFrame(schema={**SCHEMA, **TIMESTAMP_SCHEMA})
    try:
        frame = pl.read_parquet(path)
        if "announced_at" not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Datetime("us")).alias("announced_at"))
        elif isinstance(frame.schema["announced_at"], pl.Datetime) and frame.schema["announced_at"].time_zone:
            frame = frame.with_columns(pl.col("announced_at").dt.convert_time_zone(
                "Asia/Shanghai").dt.replace_time_zone(None))
        frame = frame.select(*SCHEMA, *TIMESTAMP_SCHEMA).cast({**SCHEMA, **TIMESTAMP_SCHEMA})
        if any(frame[name].null_count() for name in SCHEMA):
            raise ValueError("null evidence")
        if frame.filter(pl.col("announced_at").dt.date() != pl.col("announced_on")).height:
            raise ValueError("publication date and timestamp disagree")
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


def dated_status(history: pl.DataFrame, day: date, *, observed_at: datetime | None = None) -> pl.DataFrame:
    """Unknown publication times remain unavailable on the announcement day."""
    if history.is_empty():
        return pl.DataFrame(schema={"symbol": pl.String, DATE_COLUMN: pl.Date, STATUS_COLUMN: pl.Boolean})
    available = pl.col("announced_on") < day
    if observed_at is not None and "announced_at" in history.columns:
        from app.market_time import CN_TZ
        cutoff = observed_at.astimezone(CN_TZ).replace(tzinfo=None) if observed_at.tzinfo else observed_at
        if cutoff.date() != day:
            raise ValueError("停复牌观察时点须属于目标交易日")
        available |= ((pl.col("announced_on") == day) & (pl.col("announced_at") <= cutoff))
    return (
        history.filter((pl.col("effective_date") <= day) & available)
        .sort(["symbol", "effective_date", "announced_on"])
        .unique("symbol", keep="last", maintain_order=True)
        .select("symbol", pl.lit(day).alias(DATE_COLUMN), pl.col("is_suspended").alias(STATUS_COLUMN))
    )


def pool_evidence(repo, day: date, universe: pl.DataFrame | None = None, *,
                  observed_at: datetime | None = None) -> pl.DataFrame | None:
    """Attach status to an existing directory, or return status-only evidence."""
    getter = getattr(repo, "get_suspension_history", None)
    if not callable(getter):
        return universe
    status = dated_status(getter(), day, observed_at=observed_at)
    if status.is_empty():
        return universe
    if universe is None or universe.is_empty():
        return status
    return universe.join(status, on="symbol", how="left")
