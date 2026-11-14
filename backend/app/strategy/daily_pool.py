"""当日股票池: stock scope and status, before numeric strategy selection."""
import polars as pl

from app.strategy.basic_filter import RANGES, previous_session
from app.strategy.suspension import COLUMN, annotate

DEFINITION_VERSION = "scope_only_v3"


def scope_context(context, symbols):
    """Keep strategy signals inside their scope, with independent shared inputs."""
    from dataclasses import replace

    frames = {}
    for name in ("current", "history", "daily_history", "pool_history", "pool_universe"):
        frame = getattr(context, name)
        if frame is not None and "symbol" in frame.columns:
            frames[name] = frame.filter(pl.col("symbol").is_in(symbols))
    market = context.market
    members = set(symbols)
    if market is not None and set(market.symbols) - members:
        indices = [i for i, symbol in enumerate(market.symbols) if symbol in members]
        arrays = {name: getattr(market, name)[:, indices] for name in (
            "open", "high", "low", "close", "volume", "tradable", "limit_up_locked", "limit_down_locked",
        )}
        frames["market"] = replace(
            market, symbols=tuple(market.symbols[i] for i in indices),
            names=tuple(market.names[i] for i in indices), **arrays,
            fields={name: values[:, indices] if values.ndim == 2 else values
                    for name, values in market.fields.items()}, _valid_bars=None,
        )
    return replace(context, **frames)


def build_pool(engine, strategy_id, context, *, overrides=None, premarket=False) -> dict:
    strategy = engine.get(strategy_id)
    bf = engine._resolve_filter_scope({**(strategy.basic_filter or {}),
                                      **((overrides or {}).get("basic_filter") or {})})
    # Numeric conditions belong to strategy selection, not pool membership
    # or pool data dependencies. Work on a copy so the strategy keeps all filters.
    excluded = {f"{stem}_{side}" for stem in RANGES for side in ("min", "max")}
    bf = {key: value for key, value in bf.items() if key not in excluded | {"exclude_new_days"}}
    configured_reference = bf.get("data_reference", "signal_day")
    if premarket:
        bf = {**bf, "data_reference": "previous_trade_day"}
    history = context.pool_history
    if history is None:
        history = context.daily_history if context.timeframe == "1m" else context.history
    current = context.current
    day = context.as_of
    prior_day = previous_session(day) if bf.get("data_reference") == "previous_trade_day" else None
    parts = []
    pool_universe = context.pool_universe
    # A status-only frame supplies evidence, never additional pool membership.
    from app.instrument_suspension import DATE_COLUMN, STATUS_COLUMN
    has_universe = (pool_universe is not None and not pool_universe.is_empty()
                    and bool(set(pool_universe.columns) - {"symbol", DATE_COLUMN, STATUS_COLUMN}))
    if not premarket and has_universe:
        parts.append(pool_universe)
    if history is not None and not history.is_empty() and "date" in history.columns:
        # Missing current quotes do not remove scope members.
        parts.append(history.filter(pl.col("date") == previous_session(day)))
        if not premarket:
            parts.append(history.filter(pl.col("date") == day))
    if premarket and has_universe:
        parts.append(pool_universe)
    if current is not None and not current.is_empty():
        if "date" in current.columns:
            current = current.filter(pl.col("date") == day)
        parts.append(current)
    parts = [part for part in parts if not part.is_empty()]
    if not parts:
        raise ValueError("当日股票池缺少所需证券范围数据")
    universe = pl.concat(parts, how="diagonal_relaxed").unique("symbol", keep="last", maintain_order=True)
    universe = annotate(universe, as_of=day, evidence=pool_universe)
    scope_filter = {**bf, "enabled": True, "exclude_suspended": False}
    before_halt = engine._apply_basic_filter(universe, scope_filter, history=history, as_of=day)
    selected = (before_halt.filter(~pl.col(COLUMN).fill_null(False))
                if bf.get("exclude_suspended") else before_halt)
    symbols = sorted(set(selected["symbol"].to_list())) if "symbol" in selected.columns else []
    return {
        "strategy_id": strategy_id,
        "definition_version": DEFINITION_VERSION,
        "strategy_name": (overrides or {}).get("name") or strategy.meta.get("name", strategy_id),
        "strategy_version": strategy.logic_version,
        "as_of": str(day), "reference_date": str(prior_day or day),
        "basic_filter": bf, "symbols": symbols, "total": len(symbols),
        "suspended_count": before_halt.filter(pl.col(COLUMN) == True).height,  # noqa: E712
        "unknown_suspension_count": before_halt[COLUMN].null_count(),
        **({"configured_data_reference": configured_reference,
            "reference_policy": "premarket_previous_trade_day"} if premarket else {}),
    }
