"""Selection snapshots are independent of minute entry events."""
import polars as pl

from app.strategy import overlay
from app.strategy.basic_filter import previous_session
from app.strategy.scoring import (
    effective_scoring_directions,
    effective_strategy_scoring,
    materialize_scoring_columns,
)


def select_rows(engine, strategy, context, *, overrides, pool=None):
    bf = engine._resolve_filter_scope({**strategy.basic_filter, **overrides.get("basic_filter", {})})
    current = context.current
    history = context.daily_history
    if current is None or current.is_empty():
        raise ValueError("选股结果缺少股票范围和数值过滤快照")
    if bf.get("data_reference") == "previous_trade_day":
        from app.services.daily_close_validation import require_final_daily_rows

        source_day = previous_session(context.as_of)
        reference = history if history is not None and not history.is_empty() else current
        require_final_daily_rows(reference.filter(pl.col("date") == source_day), source_day)
        if "date" in current.columns:
            completed_current = current.filter(pl.col("date") < context.as_of)
            if not completed_current.is_empty():
                require_final_daily_rows(completed_current, source_day)
    rows = current.unique("symbol", keep="last")
    rows = engine._apply_basic_filter(rows, bf, history=history, as_of=context.as_of)
    if pool is not None:
        rows = rows.filter(pl.col("symbol").is_in(pool))
    conditions = overlay.effective_conditions(overrides)
    rows = overlay.filter_minute_candidates(rows, conditions, history=history, as_of=context.as_of)
    if rows.is_empty():
        return []

    reference_day = previous_session(context.as_of) if bf.get("data_reference") == "previous_trade_day" else context.as_of
    reference = history if reference_day != context.as_of else current
    if reference is not None and not reference.is_empty() and "date" in reference.columns:
        reference = reference.filter(pl.col("date") == reference_day)
        price = "raw_close" if "raw_close" in reference.columns else "close"
        columns = [pl.col(price).alias("reference_close")]
        if "change_pct" in reference.columns:
            columns.append(pl.col("change_pct").alias("reference_change_pct"))
        if "turnover_rate" in reference.columns:
            columns.append(pl.col("turnover_rate").alias("reference_turnover_rate"))
        if "float_shares" in reference.columns:
            columns.append((pl.col(price) * pl.col("float_shares") / 1e8).alias("reference_float_cap"))
        rows = rows.join(reference.select("symbol", *columns).unique("symbol", keep="last"),
                         on="symbol", how="left")
    rows = rows.with_columns(pl.lit(str(reference_day)).alias("reference_date"))
    if history is not None and not history.is_empty() and "amount" in history.columns:
        completed = materialize_scoring_columns(
            history.filter(pl.col("date") < context.as_of).sort("symbol", "date"),
            ["amount_mean_20d"],
        ).filter(pl.col("date") == previous_session(context.as_of))
        rows = rows.join(completed.select("symbol", "amount_mean_20d").unique("symbol", keep="last"),
                         on="symbol", how="left")

    scoring = effective_strategy_scoring(strategy.meta, overrides)
    scored = engine._apply_scoring(rows, scoring, effective_scoring_directions(overrides))
    if "score" in scored.columns:
        # Missing scoring inputs may prevent ranking, never remove a qualified stock.
        rows = rows.join(scored.select("symbol", "score"), on="symbol", how="left").sort(
            ["score", "symbol"], descending=[strategy.meta.get("descending", True), False],
            nulls_last=True,
        )
    else:
        rows = rows.sort("symbol")
    enrich = getattr(strategy, "enrich_selection_fn", None)
    if enrich is not None:
        enriched = enrich(rows.clone(), engine.resolve_params(strategy, None, overrides),
                          daily=history, as_of=context.as_of)
        if (not isinstance(enriched, pl.DataFrame) or "symbol" not in enriched.columns
                or enriched.height != rows.height
                or enriched["symbol"].n_unique() != rows.height
                or set(enriched["symbol"]) != set(rows["symbol"])):
            raise ValueError("策略预选补充指标不得增删或重复选股成员")
        # Existing eligibility/reference/score values belong to the shared path.
        extra = [name for name in enriched.columns if name not in rows.columns]
        rows = rows.join(enriched.select("symbol", *extra), on="symbol", how="left")
    return rows.to_dicts()
