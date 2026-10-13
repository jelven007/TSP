"""Pure paper-trading fee and slippage rules, shared by both storage backends."""

MIN_COMMISSION = 5.0


def apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    adjustment = 1 + slippage_bps / 10000 if side == "buy" else 1 - slippage_bps / 10000
    return round(max(price * adjustment, 0.01), 4)


def buy_fee(qty: int, price: float, commission_pct: float) -> float:
    return round(max(qty * price * commission_pct, MIN_COMMISSION), 2)


def sell_fee(qty: int, price: float, commission_pct: float, stamp_tax_pct: float) -> float:
    return round(max(qty * price * commission_pct, MIN_COMMISSION) + qty * price * stamp_tax_pct, 2)
