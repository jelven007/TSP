"""Versioned A-share sell stamp tax policy (single-sided since 2008-09-19)."""
from datetime import date

COST_MODEL_VERSION = "cn_stock_tax_minimum_commission_v2"
STAMP_TAX_REDUCTION_DATE = date(2023, 8, 28)
SINGLE_SIDED_START = date(2008, 9, 19)


def buy_quantity_rules(symbol: str, asset_type: str = "stock") -> tuple[int, int, int]:
    """Minimum, increment and per-limit-order maximum (shares/units).

    SSE STAR special trading rules: >=200, then increments of one, <=100,000.
    The existing stock universe is restricted to the four SH/SZ boards.
    """
    if asset_type == "stock" and symbol.startswith(("688", "689")):
        return 200, 1, 100_000
    return 100, 100, 1_000_000


def historical_stamp_tax_pct(trade_date: date | str, asset_type: str = "stock") -> float:
    if asset_type != "stock":
        return 0.0
    day = date.fromisoformat(str(trade_date)[:10])
    if day < SINGLE_SIDED_START:
        raise ValueError("历史印花税当前支持 2008-09-19 起的单边征收区间")
    return 0.0005 if day >= STAMP_TAX_REDUCTION_DATE else 0.001
