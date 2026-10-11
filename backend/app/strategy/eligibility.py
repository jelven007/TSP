"""Security eligibility shared by live screening and historical execution."""
from __future__ import annotations

import polars as pl

ENTRY_POLICY_VERSION = "security_eligibility_exit_first_v1"


def attach_listing_age(rows: pl.DataFrame, instruments: pl.DataFrame | None) -> pl.DataFrame:
    """Calendar days since the official IPO date; IPO day is zero.

    The status history's listing_date is deliberately not used: legacy history
    backfills stored the first available price bar there. Missing IPO metadata
    stays unknown, so an enabled new-stock filter cannot silently admit it.
    """
    if rows.is_empty() or "date" not in rows.columns:
        return rows
    if instruments is None or not {"symbol", "listing_date"} <= set(instruments.columns):
        return rows.with_columns(pl.lit(None, dtype=pl.Int32).alias("listing_age_days"))
    listing = pl.col("listing_date")
    if instruments.schema["listing_date"] == pl.String:
        listing = listing.str.to_date(strict=False)
    else:
        listing = listing.cast(pl.Date, strict=False)
    metadata = instruments.select(
        "symbol", listing.alias("_eligibility_ipo_date"),
    ).unique("symbol", keep="last")
    return (
        rows.join(metadata, on="symbol", how="left")
        .with_columns(
            (pl.col("date").cast(pl.Date) - pl.col("_eligibility_ipo_date"))
            .dt.total_days().cast(pl.Int32).alias("listing_age_days")
        )
        .drop("_eligibility_ipo_date")
    )


def new_stock_min_days(config: dict) -> int:
    return max(0, int(config.get("exclude_new_days") or 0))
