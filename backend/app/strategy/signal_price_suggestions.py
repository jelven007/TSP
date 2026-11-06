"""Frozen display suggestions for ordinary minute signal results."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.strategy.basic_filter import previous_session
from app.strategy.late_session_strength import previous_closes


def _positive(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return price if price.is_finite() and price > 0 else None


def suggested_lots(price):
    price = _positive(price)
    return int(Decimal(100000) // (price * 100)) if price is not None else None


def price_reference(context):
    return {"reference_date": str(previous_session(context.as_of)),
            "closes": previous_closes(context, exact_session=True)}


def with_price_suggestions(result, reference):
    """Enrich only display fields after storage has resolved the prior buy list."""
    current = deepcopy(result)
    if current.get("section_mode") != "signals":
        return current
    for rows in (current.get("rows", []), (current.get("sections") or {}).get("buy", [])):
        for row in rows:
            row["suggested_lots_100k"] = suggested_lots(row.get("close"))
    previous = str(previous_session(date.fromisoformat(str(current["as_of"]))))
    closes = reference.get("closes", {}) if reference.get("reference_date") == previous else {}
    for row in (current.get("sections") or {}).get("sell", []):
        price = _positive(closes.get(row.get("symbol")))
        row["suggested_sell_price"] = None if price is None else {
            "lower": float((price * Decimal(".94")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
            "upper": float((price * Decimal("1.06")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)),
            "reference_date": previous, "reference_close": float(price),
        }
    return current
