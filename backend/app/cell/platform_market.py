"""Internal read facade over the existing standardized repository/quote service."""

import math
import os
import re
import secrets
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request

from app.cell.context import SYMBOL_PATTERN

router = APIRouter(prefix="/internal/cell-market", tags=["internal-market"])


def _authorize(authorization: str | None) -> None:
    expected = os.environ.get("TSP_PLATFORM_MARKET_TOKEN", "")
    if not expected or not authorization or not secrets.compare_digest(authorization, "Bearer " + expected):
        raise HTTPException(401, "platform_authentication_required")


def _symbols(value: str) -> list[str]:
    if len(value) > 10000:
        raise HTTPException(422, "invalid_symbols")
    symbols = list(dict.fromkeys(value.split(",")))
    if not symbols or len(symbols) > 1000 or any(
        not re.fullmatch(SYMBOL_PATTERN, symbol)
        for symbol in symbols
    ):
        raise HTTPException(422, "invalid_symbols")
    return symbols


def _band(instrument: dict, field: str, day: date):
    value = instrument.get(field)
    current = str(instrument.get("as_of", ""))[:10] == str(day)
    band_day = str(instrument.get(field + "_as_of", day))[:10] == str(day)
    return value if current and band_day and isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None


@router.get("/quotes")
def quotes(
    request: Request, symbols: str,
    authorization: Annotated[str | None, Header()] = None,
):
    _authorize(authorization)
    wanted = _symbols(symbols)
    from app.market_time import cn_today

    service = getattr(request.app.state, "quote_service", None)
    if service is None:
        raise HTTPException(503, "market_unavailable")
    frame, day = service.get_enriched_today()
    if day != cn_today() or frame.is_empty():
        raise HTTPException(503, "market_unavailable")
    import polars as pl

    selected = frame.filter(pl.col("symbol").is_in(wanted))
    instruments = {row["symbol"]: row for row in request.app.state.repo.get_instruments().to_dicts()}
    status = service.status()
    rows = []
    for row in selected.to_dicts():
        # Require actual quote timestamp; never refresh a stale quote by stamping request time.
        timestamp = row.get("quote_ts") or row.get("timestamp")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            continue
        ts = float(timestamp)
        if ts > 1e12:
            ts /= 1000
        price = row.get("raw_close")
        change = row.get("change_pct")
        if not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
            continue
        if not isinstance(change, (int, float)) or not math.isfinite(change):
            continue
        try:
            source_time = datetime.fromtimestamp(ts, UTC).isoformat()
        except (OverflowError, ValueError, OSError):
            continue
        instrument = instruments.get(row["symbol"], {})
        # Instrument bands are valid only for the matching trade date.
        up, down = _band(instrument, "limit_up", day), _band(instrument, "limit_down", day)
        no_limit = up is not None and up >= 10000
        known = no_limit or (up is not None and down is not None and down < up)
        rows.append({
            "symbol": row["symbol"], "name": row.get("name") or "",
            "price": price, "change_pct": change,
            "source_time": source_time, "sequence": int(ts * 1000),
            "trading": status["is_trading_hours"], "suspended": bool(instrument.get("is_suspended", False)),
            "limit_up": None if no_limit else up, "limit_down": None if no_limit else down,
            "limits_known": known,
        })
    return {"quotes": rows}


@router.get("/history")
def history(
    request: Request, as_of: date, symbols: str, bars: int = Query(120, ge=1, le=500),
    authorization: Annotated[str | None, Header()] = None,
):
    _authorize(authorization)
    wanted = _symbols(symbols)
    from app.market_time import cn_today

    if as_of > cn_today():
        raise HTTPException(422, "future_market_date")
    repo = request.app.state.repo
    frame = repo.get_enriched_history(as_of, bars)
    if frame is None or frame.is_empty():
        raise HTTPException(503, "market_history_unavailable")
    import hashlib
    import json

    import polars as pl

    frame = frame.filter(pl.col("symbol").is_in(wanted)).filter(pl.col("date") <= as_of)
    # Repository includes an extra predecessor bar; trim to the contract's exact upper bound.
    frame = frame.sort(["symbol", "date"]).group_by("symbol", maintain_order=True).tail(bars)
    if frame.filter(pl.col("date") == as_of).is_empty():
        raise HTTPException(503, "market_history_unavailable")
    rows = [{key: None if isinstance(value, float) and not math.isfinite(value) else value
             for key, value in row.items()} for row in frame.to_dicts()]
    encoded = json.dumps(rows, default=str, sort_keys=True, allow_nan=False)
    return {
        "rows": rows, "as_of": as_of.isoformat(),
        "release_id": hashlib.sha256(encoded.encode()).hexdigest(),
        "basis": "shared_enriched_observed_snapshot",
    }
