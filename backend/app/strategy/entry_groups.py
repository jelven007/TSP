"""Optional A/B gates over a frozen five-factor selection and completed minute bars."""
from __future__ import annotations

import math
from collections import Counter
from datetime import time

import polars as pl

# Configured percentages use percentage points; archived factors use ratios.
FIELDS = {
    "score_min": ("selection_score", True, 0, 100),
    "tech_min": ("tech_score", True, 0, 100),
    "fund_min": ("fund_score", True, 0, 100),
    "momentum_min_pct": ("momentum_pct", True, -100, 10000),
    "momentum_max_pct": ("momentum_pct", False, -100, 10000),
    "bias_max_pct": ("bias_pct", False, -100, 10000),
    "slope_min_pct": ("slope_pct", True, -100, 10000),
    "rank_min": ("selection_rank", True, 1, 30),
    "rank_max": ("selection_rank", False, 1, 30),
    "breadth_min_pct": ("breadth_pct", True, 0, 100),
    "gap_min_pct": ("gap_pct", True, -100, 10000),
    "gap_max_pct": ("gap_pct", False, -100, 10000),
    "intraday_min_pct": ("intraday_pct", True, -100, 10000),
    "position_min_pct": ("position_pct", True, 0, 100),
}
OUTPUT_FIELDS = ("entry_group", "entry_priority", "selection_rank", "selection_score", "entry_fund_score")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate(groups):
    if groups is None:
        return
    if not isinstance(groups, dict) or set(groups) != {"a", "b"}:
        raise ValueError("A/B入场筛选须包含a、b两组")
    enabled = 0
    for name, group in groups.items():
        if not isinstance(group, dict) or set(group) - (set(FIELDS) | {"enabled"}):
            raise ValueError(f"{name.upper()}组参数格式错误或包含未知参数")
        if type(group.get("enabled")) is not bool:
            raise ValueError(f"{name.upper()}组须设置是否启用")
        enabled += group["enabled"]
        if group["enabled"] and not any(group.get(key) is not None for key in FIELDS):
            raise ValueError(f"{name.upper()}组启用时至少设置一个条件")
        for key, (_, _, low, high) in FIELDS.items():
            value = group.get(key)
            if value is not None and (not finite(value) or not low <= value <= high
                                      or (key.startswith("rank_") and int(value) != value)):
                raise ValueError(f"{name.upper()}组{key}须为{low}~{high}的"
                                 + ("整数或为空" if key.startswith("rank_") else "有限数值或为空"))
        for prefix in ("momentum", "rank", "gap"):
            suffix = "" if prefix == "rank" else "_pct"
            low, high = (group.get(f"{prefix}_{bound}{suffix}") for bound in ("min", "max"))
            if low is not None and high is not None and low > high:
                raise ValueError(f"{name.upper()}组{prefix}下限不得大于上限")
    if not enabled:
        raise ValueError("A/B入场筛选至少启用一组")


def needs_previous(groups):
    return bool(groups) and any(
        g["enabled"] and any(g.get(key) is not None for key in ("gap_min_pct", "gap_max_pct"))
        for g in groups.values())


def _metrics(row, bars, previous, at):
    values = {"selection_score": row.get("score"), "selection_rank": row.get("selection_rank"),
              "tech_score": row.get("tech_score"), "fund_score": row.get("fund_score")}
    inputs = row.get("scoring_inputs") or {}
    if not isinstance(inputs, dict):
        inputs = {}
    for target, source in (("momentum_pct", "momentum20"), ("bias_pct", "bias20"),
                           ("slope_pct", "ma20_change5"), ("breadth_pct", "breadth")):
        value = inputs.get(source)
        values[target] = value * 100 if finite(value) else None
    if bars is None or bars.is_empty():
        return values
    first, last = bars.row(0, named=True), bars.row(-1, named=True)
    opening, close = first.get("open"), last.get("close")
    valid_open = first["datetime"].time() == time(9, 31) and finite(opening) and opening > 0
    valid_close = last["datetime"] == at and finite(close) and close > 0
    if valid_open:
        if finite(previous) and previous > 0:
            values["gap_pct"] = (opening / previous - 1) * 100
        if valid_close:
            values["intraday_pct"] = (close / opening - 1) * 100
    if valid_close and {"high", "low"} <= set(bars.columns):
        valid = bars.select((pl.col("high").is_finite() & pl.col("low").is_finite()
                             & (pl.col("high") >= pl.col("low")) & (pl.col("low") > 0))
                            .fill_null(False).all()).item()
        if valid:
            high, low = bars["high"].max(), bars["low"].min()
            # A flat range has no defined relative position.
            if high > low and low <= close <= high:
                values["position_pct"] = (close - low) / (high - low) * 100
    return values


def match(groups, selection, frame, *, day, at, candidates, previous):
    from app.strategy.basic_filter import previous_session

    validate(groups)
    if (not isinstance(selection, dict) or selection.get("as_of") != str(day)
            or selection.get("source_date") != str(previous_session(day))
            or selection.get("scoring_policy") != "s03_five_dimensions_v1"
            or not isinstance(selection.get("rows"), list)):
        raise ValueError("A/B入场筛选缺少对应交易日的前日五维前30归档")
    rows = selection["rows"]
    if any(not isinstance(row, dict) or not isinstance(row.get("symbol"), str) for row in rows):
        raise ValueError("A/B入场筛选五维归档成员格式错误")
    counts = Counter(row["symbol"] for row in rows)
    cohort = [row for row in rows if row["symbol"] in candidates]
    parts = frame.filter(pl.col("symbol").is_in([r["symbol"] for r in cohort])).partition_by(
        "symbol", as_dict=True)
    matched, errors, usable = [], {}, 0
    for row in cohort:
        symbol = row["symbol"]
        rank = row.get("selection_rank")
        if counts[symbol] > 1 or not finite(rank) or int(rank) != rank or not 1 <= rank <= 30:
            errors[symbol] = "五维归档成员重复或排名无效"
            continue
        values = _metrics(row, parts.get((symbol,)), previous.get(symbol), at)
        computable, missing = False, []
        for name in ("a", "b"):
            group = groups[name]
            if not group["enabled"]:
                continue
            checks = [(FIELDS[key][0], FIELDS[key][1], value)
                      for key, value in group.items() if key in FIELDS and value is not None]
            required = {key for key, _, _ in checks} | ({"fund_score"} if name == "b" else set())
            absent = sorted(key for key in required if not finite(values.get(key)))
            if absent:
                missing.append(f"{name.upper()}组缺少{','.join(absent)}")
                continue
            computable = True
            if all(values[key] + 1e-9 >= bound if minimum else values[key] <= bound + 1e-9
                   for key, minimum, bound in checks):
                matched.append({"symbol": symbol, "entry_group": name.upper(),
                                "selection_rank": rank, "selection_score": row.get("score"),
                                "entry_fund_score": row.get("fund_score")})
                break
        usable += computable
        if missing:
            errors[symbol] = "; ".join(missing)
    matched.sort(key=lambda row: (
        row["entry_group"], row["selection_rank"] if row["entry_group"] == "A" else -row["entry_fund_score"],
        row["selection_rank"], row["symbol"]))
    matches = {row["symbol"]: {key: value for key, value in row.items() if key != "symbol"}
               | {"entry_priority": index + 1} for index, row in enumerate(matched)}
    return matches, errors, {"total": len(cohort), "usable": usable, "passed": len(matches),
                            "missing": len(errors)}
