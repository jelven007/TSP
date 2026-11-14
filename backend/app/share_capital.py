"""历史股本解析。

财务股本按公告日可用, 独立股本事件同时等待生效日; 缺历史时回退最新股本。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl


def load_share_history(data_dir: Path) -> pl.DataFrame:
    """读取财务股本及有独立公告证据的生效事件; 原始记录分别存储。"""
    path = data_dir / "financials" / "shares" / "part.parquet"
    frames = []
    if path.exists():
        try:
            shares = pl.read_parquet(path)
            if {"symbol", "period_end", "float_shares"} <= set(shares.columns):
                frames.append(shares)
        except Exception:
            pass
    path = data_dir / "financials/share_capital_events/part.parquet"
    if path.exists():
        try:
            events = pl.read_parquet(path)
            if {"symbol", "announce_date", "effective_date", "float_shares"} <= set(events.columns):
                # This internal ordering key is not a financial report period.
                frames.append(events.with_columns(
                    pl.col("effective_date").alias("period_end"),
                ))
        except Exception:
            pass
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def apply_historical_float_shares(
    rows: pl.DataFrame,
    shares: pl.DataFrame | None,
    *,
    today: date,
) -> pl.DataFrame:
    """为行情行解析有效流通股本。

    当日保留 rows.float_shares; 历史日期使用已公告且已生效的最新股本,
    找不到历史记录时继续使用 rows.float_shares。
    """
    required = {"symbol", "date", "float_shares"}
    if (
        rows.is_empty()
        or not required <= set(rows.columns)
        or shares is None
        or shares.is_empty()
        or not {"symbol", "period_end", "float_shares"} <= set(shares.columns)
    ):
        return rows

    def as_date_expr(column: str) -> pl.Expr:
        dtype = shares.schema[column]
        if dtype == pl.Utf8:
            return pl.col(column).str.to_date(strict=False)
        return pl.col(column).cast(pl.Date, strict=False)

    available_date = as_date_expr("period_end")
    if "announce_date" in shares.columns:
        available_date = as_date_expr("announce_date").fill_null(available_date)
    if "effective_date" in shares.columns:
        available_date = pl.max_horizontal(available_date, as_date_expr("effective_date"))

    history = (
        shares
        .select(
            pl.col("symbol").cast(pl.Utf8),
            available_date.alias("_share_available_date"),
            pl.col("period_end").cast(pl.Utf8).alias("_share_period_end"),
            pl.col("float_shares").cast(pl.Float64, strict=False).alias("_historical_float_shares"),
        )
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("_share_available_date").is_not_null()
            & (pl.col("_historical_float_shares") > 0)
        )
        .sort(["symbol", "_share_available_date", "_share_period_end"])
        .unique(subset=["symbol", "_share_available_date"], keep="last")
        .sort(["symbol", "_share_available_date"])
    )
    if history.is_empty():
        return rows

    resolved = (
        rows
        .with_row_index("_share_row_order")
        .with_columns(
            pl.col("symbol").cast(pl.Utf8),
            pl.col("date").cast(pl.Date, strict=False).alias("_share_trade_date"),
        )
        .sort(["symbol", "_share_trade_date"])
        .join_asof(
            history,
            left_on="_share_trade_date",
            right_on="_share_available_date",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
        .with_columns(
            pl.when(pl.col("_share_trade_date") == pl.lit(today))
            .then(pl.col("float_shares"))
            .otherwise(
                pl.coalesce("_historical_float_shares", "float_shares")
            )
            .alias("float_shares")
        )
        .sort("_share_row_order")
    )
    return resolved.drop(
        "_share_row_order",
        "_share_trade_date",
        "_share_available_date",
        "_share_period_end",
        "_historical_float_shares",
    )
