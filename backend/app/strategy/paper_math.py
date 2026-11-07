"""Pure paper-trading fee and slippage rules, shared by both storage backends."""

MIN_COMMISSION = 5.0


def apply_slippage(price: float, side: str, slippage_bps: float) -> float:
    adjustment = 1 + slippage_bps / 10000 if side == "buy" else 1 - slippage_bps / 10000
    return round(max(price * adjustment, 0.01), 4)


def buy_fee(qty: int, price: float, commission_pct: float, *,
            min_commission: float = MIN_COMMISSION, transfer_fee_pct: float = 0) -> float:
    gross = qty * price
    return round(max(gross * commission_pct, min_commission) + gross * transfer_fee_pct, 2)


def sell_fee(qty: int, price: float, commission_pct: float, stamp_tax_pct: float, *,
             min_commission: float = MIN_COMMISSION, transfer_fee_pct: float = 0) -> float:
    gross = qty * price
    return round(max(gross * commission_pct, min_commission) + gross * (stamp_tax_pct + transfer_fee_pct), 2)


def account_fee(account: dict, qty: int, price: float, side: str, *,
                day: str | None = None, asset_type: str = "stock") -> float:
    """预算和单笔真实成交共用费表。模拟撮合每单全量成交, 只收一次最低佣金。"""
    options = {"min_commission": account.get("min_commission", MIN_COMMISSION),
               "transfer_fee_pct": account.get("transfer_fee_pct", 0)}
    if side == "buy":
        return buy_fee(qty, price, account["commission_pct"], **options)
    tax = account["stamp_tax_pct"]
    if account.get("stamp_tax_mode", "fixed") == "historical":
        from app.backtest.costs import historical_stamp_tax_pct

        tax = historical_stamp_tax_pct(day, asset_type)
    return sell_fee(qty, price, account["commission_pct"], tax, **options)
