"""候选默认序的执行投影; 保留完整原始名单和 selection_rank。"""
from __future__ import annotations

import math


def candidate_pool_order(result, strategy_id: str, params: dict) -> dict[str, dict]:
    projected = getattr(result, "candidate_pool_metadata", None)
    if projected is not None:
        return projected
    sections = getattr(result, "sections", None)
    rows = sections.get("select", []) if sections is not None else result.rows
    mode = getattr(result, "section_mode", None)
    if sections is not None and (getattr(result, "section_errors", None) or {}).get("select"):
        return {}
    # Never recover rank from display-truncated input.
    snapshot = getattr(result, "selection_snapshot", None) or {}
    if snapshot.get("total", len(rows)) != len(rows):
        return {}
    if sections is None and getattr(result, "total", len(rows)) != len(rows):
        return {}
    rule = "output_order_v1"
    metric = None
    if mode == "signals" and strategy_id == "custom_mvxxcp88":
        metric, rule = "atr14_previous_close", "s02_atr14_previous_close_desc_v1"
    elif sections is not None and mode != "signals":
        limit = params.get("position_limit", 5)
        if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
            limit = 5
        metric = "return_1430" if len(rows) > limit else "prev_d_amount20"
        rule = f"{metric}_desc_v1"
    ordered = list(rows)
    if metric:
        def key(row):
            value = row.get(metric)
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            return (-float(value) if valid else math.inf, str(row.get("symbol", "")))
        ordered.sort(key=key)
    symbols = [str(row.get("symbol", "")) for row in ordered]
    if any(not symbol for symbol in symbols) or len(set(symbols)) != len(symbols):
        return {}
    source = {"strategy_id": strategy_id, "as_of": str(result.as_of), "size": len(symbols)}
    return {
        symbol: {"candidate_pool_rank": rank, "candidate_pool_source": source,
                 "candidate_pool_rule": rule}
        for rank, symbol in enumerate(symbols, 1)
    }
