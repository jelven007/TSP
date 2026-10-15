"""Validated entry conditions shared by screening, monitoring and research."""
from __future__ import annotations

import math
from collections.abc import Mapping

import polars as pl

from app.strategy import custom_signals
from app.strategy.scoring import (
    materialize_scoring_columns,
    scoring_dependencies,
    scoring_warmup_bars,
)


class OverlayDataError(ValueError):
    """Missing entry-filter inputs; explicit exits must remain usable."""


def normalize_conditions(conditions: object) -> list[dict]:
    if conditions is None:
        return []
    if not isinstance(conditions, list) or len(conditions) > 8:
        raise ValueError("overlay_filter 必须是列表, 最多 8 条条件")
    if not conditions:
        return []
    custom_signals.validate({
        "id": "overlay", "name": "入场叠加条件", "kind": "entry", "conditions": conditions,
    })
    result = []
    for i, condition in enumerate(conditions):
        item = {key: condition[key] for key in ("left", "op", "right")}
        for key in ("leftDays", "rightDays"):
            raw = condition.get(key, 0)
            if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
                raise ValueError(f"第 {i + 1} 个条件: {key} 必须是整数")
            item[key] = custom_signals._parse_days(condition, key, i)
        is_string = item["left"] in custom_signals._string_ext_fields()
        kind, value = custom_signals._parse_right(item["right"], string_mode=is_string)
        if kind == "const" and (
            isinstance(item["right"], bool) or not math.isfinite(value)
        ):
            raise ValueError(f"第 {i + 1} 个条件: 右值必须是有限数值")
        if kind == "field" and value in custom_signals._string_ext_fields():
            raise ValueError("数值条件不能引用字符串右值字段")
        item["right"] = f"field:{value}" if kind == "field" else str(value)
        result.append(item)
    return result


def effective_conditions(overrides: Mapping | None) -> list[dict]:
    return normalize_conditions((overrides or {}).get("overlay_filter"))


def fields(conditions: list[dict]) -> set[str]:
    result = {c["left"] for c in conditions}
    result.update(
        c["right"][6:] for c in conditions
        if isinstance(c["right"], str) and c["right"].startswith("field:")
    )
    return result


def dependencies(conditions: list[dict]) -> set[str]:
    return scoring_dependencies(dict.fromkeys(fields(conditions), 1))


def warmup_bars(conditions: list[dict]) -> int:
    from app.indicators.pipeline import INDICATOR_COLUMNS

    def field_warmup(name):
        return max(
            scoring_warmup_bars({name: 1}),
            61 if ({name} | scoring_dependencies({name: 1})) & set(INDICATOR_COLUMNS) else 1,
        )

    return max((
        field_warmup(name) + int(c.get(key, 0))
        for c in conditions
        for name, key in (
            (c["left"], "leftDays"),
            (c["right"][6:] if isinstance(c["right"], str)
             and c["right"].startswith("field:") else c["left"], "rightDays"),
        )
    ), default=1)


def frame_mask(frame: pl.DataFrame, conditions: object) -> pl.Series:
    conditions = normalize_conditions(conditions)
    if not conditions:
        return pl.Series("_overlay", [True] * frame.height, dtype=pl.Boolean)
    requested = fields(conditions)
    timestamp = "datetime" if "datetime" in frame.columns else "date"
    order = [key for key in ("symbol", timestamp) if key in frame.columns]
    ordered = frame.with_row_index("_overlay_row").sort(order) if order else frame.with_row_index("_overlay_row")
    ordered = materialize_scoring_columns(ordered, sorted(requested))
    missing = requested - set(ordered.columns)
    if missing:
        raise OverlayDataError(f"入场叠加条件缺少字段: {', '.join(sorted(missing))}")
    parts = []
    for c in conditions:
        left = custom_signals._col(c["left"], c["leftDays"])
        is_string = c["left"] in custom_signals._string_ext_fields()
        kind, value = custom_signals._parse_right(c["right"], string_mode=is_string)
        right = custom_signals._col(value, c["rightDays"]) if kind == "field" else pl.lit(value)
        part = custom_signals._OP_BUILDERS[c["op"]](left, right).fill_null(False)
        if not is_string:
            part = part & left.is_finite().fill_null(False) & right.is_finite().fill_null(False)
        parts.append(part)
    return ordered.with_columns(
        pl.all_horizontal(parts).alias("_overlay"),
    ).sort("_overlay_row")["_overlay"]


def filter_candidates(
    candidates: pl.DataFrame,
    conditions: list[dict],
    *,
    history: pl.DataFrame | None = None,
    current: pl.DataFrame | None = None,
) -> pl.DataFrame:
    if not conditions or candidates.is_empty():
        return candidates
    source = history if history is not None and not history.is_empty() else current
    if source is None:
        source = candidates
    if current is not None and not current.is_empty() and history is not None:
        # Live bars replace historical rows at the same timestamp.
        keys = [key for key in ("symbol", "date") if key in source.columns and key in current.columns]
        source = pl.concat([source.join(current.select(keys).unique(), on=keys, how="anti"), current],
                           how="diagonal_relaxed") if keys else source
    if warmup_bars(conditions) > 1 and history is None:
        raise OverlayDataError("入场叠加条件需要历史窗口")
    allowed = source.filter(frame_mask(source, conditions))
    keys = [key for key in ("symbol", "date") if key in candidates.columns and key in allowed.columns]
    if "symbol" not in keys:
        raise ValueError("入场叠加条件缺少证券标识")
    return candidates.join(allowed.select(keys).unique(), on=keys, how="semi")
