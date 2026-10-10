"""Versioned A-share sell stamp tax policy (single-sided since 2008-09-19)."""
from datetime import date

COST_MODEL_VERSION = "cn_stock_tax_v1"
STAMP_TAX_REDUCTION_DATE = date(2023, 8, 28)
SINGLE_SIDED_START = date(2008, 9, 19)


def historical_stamp_tax_pct(trade_date: date | str, asset_type: str = "stock") -> float:
    if asset_type != "stock":
        return 0.0
    day = date.fromisoformat(str(trade_date)[:10])
    if day < SINGLE_SIDED_START:
        raise ValueError("历史印花税当前支持 2008-09-19 起的单边征收区间")
    return 0.0005 if day >= STAMP_TAX_REDUCTION_DATE else 0.001
