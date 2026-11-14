"""Internal A-share one-minute close-clock contract, independent of providers."""
from datetime import date, datetime, timedelta

import polars as pl

CLOCK_CONTRACT = {
    "schema": "minute_close_clock_v1",
    "timestamp_label": "close",
    "timezone": "Asia/Shanghai",
    "morning": ["09:31:00", "11:30:00"],
    "afternoon": ["13:01:00", "15:00:00"],
    "complete_session_bars": 240,
}


def closed_minute_stamps(day: date) -> list[datetime]:
    midnight = datetime.combine(day, datetime.min.time())
    return [midnight + timedelta(minutes=m)
            for m in (*range(571, 691), *range(781, 901))]


def normalize_minute_clock(
    frame: pl.DataFrame, *, timestamp_label: str = "close",
) -> pl.DataFrame:
    """Consume an explicit start label once; canonical consumers use close."""
    if timestamp_label not in ("close", "start"):
        raise ValueError("minute_timestamp_label must be 'close' or 'start'")
    if frame.is_empty():
        return frame
    dtype = frame.schema.get("datetime")
    if not isinstance(dtype, pl.Datetime):
        raise ValueError("1分钟K收盘时标须为有效北京时间 datetime")
    if dtype.time_zone is not None:
        frame = frame.with_columns(
            pl.col("datetime").dt.convert_time_zone("Asia/Shanghai")
            .dt.replace_time_zone(None),
        )
    if timestamp_label == "start":
        frame = frame.with_columns(pl.col("datetime") + pl.duration(minutes=1))
    minute = pl.col("datetime").dt.hour().cast(pl.Int32) * 60 + pl.col("datetime").dt.minute()
    valid = (
        (((minute >= 571) & (minute <= 690)) | ((minute >= 781) & (minute <= 900)))
        & (pl.col("datetime").dt.second() == 0)
        & (pl.col("datetime").dt.nanosecond() == 0)
    ).fill_null(False)
    if not frame.select(valid.all()).item():
        bad = frame.filter(~valid)["datetime"].head(2).to_list()
        raise ValueError(
            f"1分钟K收盘时标须为09:31~11:30、13:01~15:00整分: {bad}"
        )
    return frame
