"""Conservative, single-session A-share execution validation on sampled quotes."""
from __future__ import annotations

import hashlib
import io
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import polars as pl

from app.backtest.costs import COST_MODEL_VERSION, historical_stamp_tax_pct
from app.backtest.second_replay import SecondQuoteReplay, SecondReplayDataError, session_windows
from app.instrument_history import (
    HISTORY_SCHEMA_VERSION,
    InstrumentHistoryError,
    history_path,
    load_instrument_history,
)
from app.market_scope import supported_symbol
from app.market_time import CN_TZ
from app.price_limits import is_no_limit_day, price_limit_pct


@dataclass(frozen=True)
class SecondOrder:
    order_id: str
    symbol: str
    signal_ms: int
    side: Literal["buy", "sell"]
    shares: int


@dataclass(frozen=True)
class OpeningLot:
    symbol: str
    shares: int
    acquired_date: date


@dataclass(frozen=True)
class ExecutionConfig:
    initial_cash: float = 100_000.0
    latency_ms: int = 300
    expiry_ms: int = 10_000
    max_gap_ms: int = 1500
    max_source_age_ms: int = 5000
    participation: float = 0.01
    commission_pct: float = 0.0002
    minimum_commission: float = 5.0
    slippage_bps: float = 5.0
    exclude_st: bool = True


def _status(history: pl.DataFrame, symbol: str, signal_ms: int) -> dict | None:
    now = datetime.fromtimestamp(signal_ms / 1000, CN_TZ)
    matches = history.filter(
        (pl.col("symbol") == symbol)
        & (pl.col("valid_from") <= now.date())
        & (pl.col("valid_to").is_null() | (pl.col("valid_to") > now.date()))
    )
    if matches.height != 1:
        return None
    row = matches.row(0, named=True)
    if (
        row["schema_version"] != HISTORY_SCHEMA_VERSION
        or not row["name"] or row["source"] in (None, "", "unknown")
    ):
        return None
    try:
        available = datetime.fromisoformat(row["available_at"].replace("Z", "+00:00"))
        if available.tzinfo is None or available > now:
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    return row


def validate_execution(
    data_dir: Path, day: date, *, start_ms: int, end_ms: int,
    orders: list[SecondOrder], opening_lots: list[OpeningLot] | None = None,
    config: ExecutionConfig | None = None,
) -> dict:
    """Validate supplied signals; no strategy generation, queue model, or return forecast."""
    config = config or ExecutionConfig()
    if not 1 <= len(orders) <= 1000 or len({order.order_id for order in orders}) != len(orders):
        raise ValueError("provide 1-1000 uniquely identified orders")
    if not math.isfinite(config.initial_cash) or config.initial_cash < 0:
        raise ValueError("initial cash must be finite and nonnegative")
    if not 0 <= config.latency_ms < config.expiry_ms <= 60_000:
        raise ValueError("require 0 <= latency_ms < expiry_ms <= 60000")
    if not 0 < config.max_gap_ms <= 60_000 or not 0 <= config.max_source_age_ms <= 60_000:
        raise ValueError("invalid quote freshness limits")
    if not math.isfinite(config.participation) or not 0 < config.participation <= 0.1:
        raise ValueError("participation must be in (0, 0.1]")
    if any(not math.isfinite(value) or value < 0 for value in (
        config.commission_pct, config.minimum_commission, config.slippage_bps,
    )):
        raise ValueError("costs must be finite and nonnegative")
    # One continuous session avoids overnight corporate actions and lunch fills.
    if not any(lo <= start_ms < end_ms <= hi for lo, hi in session_windows(day)):
        raise ValueError("execution window must be within one continuous auction session")
    for order in orders:
        if not order.order_id or not supported_symbol(order.symbol, "stock"):
            raise ValueError("orders require IDs and supported A-share symbols")
        minimum = 200 if order.symbol.startswith(("688", "689")) and order.side == "buy" else 100
        if order.side not in {"buy", "sell"} or order.shares < minimum or order.shares % 100:
            raise ValueError("this verifier supports board-compliant whole-lot orders only")
        if not start_ms <= order.signal_ms < end_ms:
            raise ValueError("signal outside execution window")
    lots = [asdict(lot) for lot in (opening_lots or [])]
    if any(
        lot["acquired_date"] > day or lot["shares"] <= 0
        or not supported_symbol(lot["symbol"], "stock")
        for lot in lots
    ):
        raise ValueError("invalid opening lots")

    replay = SecondQuoteReplay(data_dir)
    symbols = sorted({order.symbol for order in orders})
    quality = replay.validate(
        day, day, symbols=symbols, start_ms=start_ms, end_ms=end_ms,
        max_source_age_ms=config.max_source_age_ms,
    )
    if quality["status"] != "ready":
        raise SecondReplayDataError(f"second quote window failed quality checks: {quality['issues']}")
    status_file = history_path(data_dir)
    try:
        status_bytes = status_file.read_bytes() if status_file.exists() else b""
        history = load_instrument_history(data_dir)
        if status_bytes:
            # The shared compatibility loader fills null flags with False.
            # Research must not turn an unknown ST state into a normal stock.
            flags = pl.read_parquet(io.BytesIO(status_bytes), columns=["is_risk_warning", "is_listed"])
            if any(dtype != pl.Boolean for dtype in flags.dtypes) or any(flags.null_count().row(0)):
                raise SecondReplayDataError("instrument history has unknown status flags")
        if (status_file.read_bytes() if status_file.exists() else b"") != status_bytes:
            raise SecondReplayDataError("instrument history changed during validation")
    except (OSError, InstrumentHistoryError, pl.exceptions.PolarsError) as exc:
        raise SecondReplayDataError(f"invalid instrument history: {exc}") from exc
    history_digest = hashlib.sha256(status_bytes).hexdigest()
    outcomes = {}
    for order in orders:
        state = _status(history, order.symbol, order.signal_ms)
        reason = None
        if state is None or state["listing_date"] is None or state["listing_date"] > day:
            reason = "historical_status_unknown"
        elif not state["is_listed"]:
            reason = "not_listed"
        elif config.exclude_st and state["is_risk_warning"]:
            reason = "risk_warning_excluded"
        elif is_no_limit_day(order.symbol, state["listing_date"], day):
            reason = "new_listing_window_unsupported"
        outcomes[order.order_id] = {
            **asdict(order), "status": "rejected" if reason else "pending",
            "reason": reason or "no_eligible_quote", "_state": state,
        }

    cash = config.initial_cash
    previous: dict[str, tuple[int, dict]] = {}
    fills = []
    for snapshot in replay.iter_snapshots(day, day, symbols=symbols, asset_type="stock", start_ms=start_ms, end_ms=end_ms):
        if not any(lo <= snapshot.collected_at_ms < hi for lo, hi in session_windows(day)):
            continue
        current = {row["symbol"]: row for row in snapshot.frame.iter_rows(named=True)}
        consumed: dict[str, int] = {}
        book_consumed: dict[tuple[str, str], int] = {}
        for order in sorted(orders, key=lambda item: (item.signal_ms, item.order_id)):
            result = outcomes[order.order_id]
            if result["status"] != "pending":
                continue
            now = snapshot.collected_at_ms
            if now > order.signal_ms + config.expiry_ms:
                result.update(status="rejected", reason="expired")
                continue
            if now <= order.signal_ms or now < order.signal_ms + config.latency_ms:
                continue
            row = current.get(order.symbol)
            prior = previous.get(order.symbol)
            if row is None or prior is None or not 0 < now - prior[0] <= config.max_gap_ms:
                result["reason"] = "missing_adjacent_quote"
                continue
            source, prior_source = row["source_ts_ms"], prior[1]["source_ts_ms"]
            if source is None or prior_source is None or source <= prior_source or not 0 <= now - source <= config.max_source_age_ms:
                result["reason"] = "stale_source_quote"
                continue
            # A delta spanning order arrival includes pre-order trades. Wait for
            # a fully observable post-arrival interval instead of prorating it.
            arrival = order.signal_ms + config.latency_ms
            if prior[0] < arrival or prior_source < arrival:
                result["reason"] = "no_post_arrival_interval"
                continue
            volume_delta = row["volume"] - prior[1]["volume"]
            amount_delta = row["amount"] - prior[1]["amount"]
            side = "ask1" if order.side == "buy" else "bid1"
            book, book_lots = row[side], row[f"{side}_volume"]
            opposite = row["bid1" if order.side == "buy" else "ask1"]
            if book is None or book_lots is None or not math.isfinite(book) or not math.isfinite(book_lots) or book <= 0 or book_lots <= 0:
                result["reason"] = "empty_book"
                continue
            if opposite is None or not math.isfinite(opposite) or opposite <= 0 or row["bid1"] > row["ask1"]:
                result["reason"] = "locked_or_crossed_book"
                continue
            if volume_delta <= 0 or amount_delta <= 0:
                result["reason"] = "no_new_trading_volume"
                continue
            capacity = min(
                math.floor(volume_delta * 100 * config.participation) - consumed.get(order.symbol, 0),
                math.floor(book_lots * 100) - book_consumed.get((order.symbol, side), 0),
            )
            if capacity < order.shares:
                result["reason"] = "insufficient_sampled_liquidity"
                continue
            previous_close = row["prev_close"]
            if previous_close is None or not math.isfinite(previous_close) or previous_close <= 0:
                result["reason"] = "invalid_previous_close"
                continue
            limit = price_limit_pct(order.symbol, day, is_risk_warning=result["_state"]["is_risk_warning"])
            cents = math.floor(previous_close * 100 + 0.5)
            upper = ((cents * round((1 + limit) * 100) + 50) // 100) / 100
            lower = ((cents * round((1 - limit) * 100) + 50) // 100) / 100
            adjusted = book * (1 + (1 if order.side == "buy" else -1) * config.slippage_bps / 10_000)
            price = (math.ceil(adjusted * 100 - 1e-8) if order.side == "buy" else math.floor(adjusted * 100 + 1e-8)) / 100
            if not lower < price < upper:
                result["reason"] = "price_limit"
                continue
            notional = round(price * order.shares, 2)
            commission = round(max(config.minimum_commission, notional * config.commission_pct), 2)
            tax = round(notional * historical_stamp_tax_pct(day), 2) if order.side == "sell" else 0.0
            if order.side == "buy":
                if cash < notional + commission:
                    result.update(status="rejected", reason="insufficient_cash")
                    continue
                cash -= notional + commission
                lots.append({"symbol": order.symbol, "shares": order.shares, "acquired_date": day})
            else:
                sellable = [lot for lot in lots if lot["symbol"] == order.symbol and lot["acquired_date"] < day]
                if sum(lot["shares"] for lot in sellable) < order.shares:
                    result.update(status="rejected", reason="t_plus_one_or_insufficient_position")
                    continue
                remaining = order.shares
                for lot in sellable:
                    taken = min(remaining, lot["shares"])
                    lot["shares"] -= taken
                    remaining -= taken
                cash += notional - commission - tax
            consumed[order.symbol] = consumed.get(order.symbol, 0) + order.shares
            book_consumed[(order.symbol, side)] = book_consumed.get((order.symbol, side), 0) + order.shares
            result.update(status="filled", reason=None)
            fills.append({
                "order_id": order.order_id, "symbol": order.symbol, "side": order.side,
                "signal_ms": order.signal_ms, "fill_ms": now, "capture_id": str(snapshot.capture_id),
                "shares": order.shares, "price": price, "commission": commission, "stamp_tax": tax,
            })
        previous.update({symbol: (snapshot.collected_at_ms, row) for symbol, row in current.items()})
    for result in outcomes.values():
        result.pop("_state")
        if result["status"] == "pending":
            result["status"] = "unfilled"
    request = {
        "day": day.isoformat(), "start_ms": start_ms, "end_ms": end_ms,
        "orders": [asdict(order) for order in orders], "config": asdict(config),
        "opening_lots": [asdict(lot) for lot in (opening_lots or [])],
    }
    return {
        "model": "sampled_l1_conservative_v1", "cost_model": COST_MODEL_VERSION,
        "quality": quality, "data_manifest": replay.manifest(day, day),
        "instrument_history_sha256": history_digest,
        "config_hash": hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest(),
        "orders": list(outcomes.values()), "fills": fills,
        "ending_cash": round(cash, 2), "ending_lots": [lot for lot in lots if lot["shares"] > 0],
        "exchange_fill_guaranteed": False,
    }
