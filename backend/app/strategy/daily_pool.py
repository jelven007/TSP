"""当日股票池: stock scope plus float-cap bounds, before strategy selection."""
import polars as pl

from app.strategy.basic_filter import RANGES, fields, previous_session, uses_previous
from app.strategy.suspension import COLUMN, annotate

DEFINITION_VERSION = "scope_float_cap_v2"


def build_pool(engine, strategy_id, context, *, overrides=None, premarket=False) -> dict:
    strategy = engine.get(strategy_id)
    bf = engine._resolve_filter_scope({**(strategy.basic_filter or {}),
                                      **((overrides or {}).get("basic_filter") or {})})
    # Other numeric conditions belong to strategy selection, not pool membership
    # or pool data dependencies. Work on a copy so the strategy keeps all filters.
    excluded = {f"{stem}_{side}" for stem in RANGES if stem != "float_cap" for side in ("min", "max")}
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
        # The universe does not shrink just because a halted stock has no
        # current bar. Signal-day float-cap bounds still require today's values.
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
        raise ValueError("当日股票池缺少所需行情数据")
    reference = history if uses_previous(bf) else current
    if fields(bf):
        if reference is None or reference.is_empty():
            raise ValueError("当日股票池缺少数值过滤参考数据")
        if "date" in reference.columns:
            reference = reference.filter(pl.col("date") == (prior_day if uses_previous(bf) else day))
        if reference.is_empty():
            raise ValueError("当日股票池缺少精确参考交易日行情")
        for name in fields(bf):
            source_name = "close" if name == "raw_close" and name not in reference.columns else name
            if source_name not in reference.columns or reference[source_name].null_count() == reference.height:
                raise ValueError(f"当日股票池参考数据缺少{name}")
    universe = pl.concat(parts, how="diagonal_relaxed").unique("symbol", keep="last", maintain_order=True)
    universe = annotate(universe, as_of=day, evidence=pool_universe)
    # Signal-day float cap cannot use yesterday's row; previous-day reference can
    # retain a qualifying halted stock even when it has no current quote.
    if fields(bf) and not uses_previous(bf) and "date" in universe.columns:
        universe = universe.filter(pl.col("date") == day)
    scope_filter = {**bf, "enabled": True, "exclude_suspended": False}
    if not bf.get("enabled", True):
        # Legacy board/ST scope must apply even when numeric filters are disabled.
        scope_filter.update(float_cap_min=None, float_cap_max=None)
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
