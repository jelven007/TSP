"""S05 entry routing, configured signals and order windows."""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime, time, timedelta

import polars as pl

from app.market_time import as_cn_datetime
from app.strategy.basic_filter import previous_session

SID = "custom_mvxxcs05"
POLICY = "s02_173004552_v1"
BRANCHES = ("tail", "low_gap", "board")
ENTRY_IDS = {name: f"{name}_entry_s05" for name in BRANCHES}
EXIT_IDS = {name: f"{name}_exit_s05" for name in BRANCHES}
EPSILON = 1e-10
PARTICIPATION = .10
ENTRY_SORTS = ("return_5m", "fund_tech", "four_dimension")
LEGACY_MARKET_ROUTE = "s05_market_route_v2"
MARKET_ROUTE = "s05_daily_branch_v3"
BRANCH_CODES = {1: "tail", 2: "low_gap", 3: "board", 4: None}
MARKET_DEFAULTS = {
    "tail": {"prior_up_5d_min_pct": 55, "prior_up_vs_5d_max_pp": -35},
    "low_gap": {"prior_up_vs_20d_min_pp": -32, "prior_up_2d_min_pct": 50},
    "board": {"prior_up_min_pct": 48, "prior_above_ma20_min_pct": 45},
}
NO_MARKET_ROUTE = "三个市场分支均未满足, 当日不新买"


def with_entry_branches(rows, hits):
    """Read only unambiguous per-symbol evidence from the original buy."""
    by_signal = {"csgi_" + value: branch for branch, value in ENTRY_IDS.items()}
    branches = {}
    for hit in hits:
        branches.setdefault(hit["symbol"], set()).update(
            by_signal[name] for name in hit.get("signals", []) if name in by_signal)
    output = []
    for row in rows:
        evidence = set(branches.get(row["symbol"], ()))
        if row.get("entry_branch") in BRANCHES:
            evidence.add(row["entry_branch"])
        output.append({**row, "entry_branch": next(iter(evidence)) if len(evidence) == 1 else None})
    return output


# Fallbacks for definitions saved before the entry parameters were exposed.
ENTRY_DEFAULTS = {
    "tail": {"return_5m_min_pct": 0, "return_15m_min_pct": .1,
             "change_min_pct": 0, "change_max_pct": 7, "sort_by": "return_5m"},
    "low_gap": {"gap_max_pct": 1, "change_min_pct": -1, "change_max_pct": 6,
                "position_min_pct": 65, "return_15m_min_pct": -.15,
                "veto_change_min_pct": 2.5, "sort_by": "fund_tech"},
    "board": {"change_max_pct": 7, "sort_by": "four_dimension"},
}


def enabled(strategy) -> bool:
    return strategy.meta.get("id") == SID and strategy.meta.get("entry_routing") == POLICY


def _number(value, minimum=-math.inf, maximum=math.inf):
    return (type(value) in (int, float) and math.isfinite(value)
            and minimum <= value <= maximum)


def sessions_until(day):
    days = []
    for _ in range(21):
        day = previous_session(day)
        days.append(day)
    return list(reversed(days))


def _market_matches(evidence, day, thresholds):
    """Use exactly T-21 through T-1, including the 20th lag."""
    if not isinstance(evidence, list) or len(evidence) != 21:
        raise ValueError("S05分流缺少精确21个交易日市场广度")
    for row, expected in zip(evidence, sessions_until(day), strict=True):
        if (not isinstance(row, dict) or row.get("date") != str(expected)
                or not _number(row.get("up"), 0, 1) or not _number(row.get("above_ma20"), 0, 1)):
            raise ValueError(f"S05分流市场广度日期或数值无效: {expected}")
    values = [row["up"] for row in evidence]
    if not isinstance(thresholds, dict) or thresholds.keys() != MARKET_DEFAULTS.keys():
        raise ValueError("S05分流门槛缺失")
    for branch, defaults in MARKET_DEFAULTS.items():
        raw = thresholds[branch]
        if not isinstance(raw, dict) or raw.keys() != defaults.keys():
            raise ValueError("S05分流门槛缺失")
        entry_parameters(branch, raw)
    tail, low_gap, board = (thresholds[branch] for branch in BRANCHES)
    conditions = (
        math.fsum(values[-5:]) / 5 >= tail["prior_up_5d_min_pct"] / 100 - EPSILON
        or values[-1] - values[-6] <= tail["prior_up_vs_5d_max_pp"] / 100 + EPSILON,
        values[-1] - values[0] >= low_gap["prior_up_vs_20d_min_pp"] / 100 - EPSILON
        and math.fsum(values[-2:]) / 2 >= low_gap["prior_up_2d_min_pct"] / 100 - EPSILON,
        values[-1] >= board["prior_up_min_pct"] / 100 - EPSILON
        and evidence[-1]["above_ma20"] >= board["prior_above_ma20_min_pct"] / 100 - EPSILON,
    )
    return [branch for branch, matched in zip(BRANCHES, conditions, strict=True) if matched]


def route(evidence: list[dict], day: date) -> str:
    """Original research/legacy archive route; its board gate ran intraday."""
    return next(iter(_market_matches(evidence, day, MARKET_DEFAULTS)), "board")


def market_thresholds(definitions, *, configured_only=False):
    book = {sig["id"]: sig for sig in definitions}
    thresholds = {}
    for branch, fields in MARKET_DEFAULTS.items():
        stored = (book.get(ENTRY_IDS[branch], {}).get("params") or {}).get("s05_entry", {})
        effective = entry_parameters(branch, stored)
        if configured_only and not fields.keys() <= stored.keys():
            return None
        thresholds[branch] = {key: effective[key] for key in fields}
    return thresholds


def freeze_market_route(selection, day, thresholds, *, observed):
    """Freeze the market decision, separate from the stock selection and live evaluation."""
    matches = _market_matches(selection.get("market_breadth"), day, thresholds)
    branch = next(iter(matches), None)
    code = next(code for code, value in BRANCH_CODES.items() if value == branch)
    return {**selection, "entry_branch": branch, "branch_code": code,
            "entry_route": {"schema": MARKET_ROUTE, "thresholds": thresholds, "matched": matches,
                            "decided_at": as_cn_datetime(observed).isoformat()}}


def frozen_route(selection: dict, day: date) -> str | None:
    """Bind the trading day, original previous day, breadth and selected branch."""
    if (not isinstance(selection, dict) or selection.get("as_of") != str(day)
            or selection.get("source_date") != str(previous_session(day))
            or selection.get("entry_routing") != POLICY or not isinstance(selection.get("rows"), list)):
        raise ValueError("缺少目标日S02四维分流归档")
    if "entry_route" not in selection:
        branch = route(selection.get("market_breadth"), day)
    else:
        decision = selection["entry_route"]
        if not isinstance(decision, dict) or decision.get("schema") not in (MARKET_ROUTE, LEGACY_MARKET_ROUTE):
            raise ValueError("S05分流归档口径无效")
        try:
            decided = datetime.fromisoformat(decision["decided_at"])
            if decided.tzinfo is None:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError("S05分流归档缺少有效判断时间") from None
        matches = _market_matches(selection.get("market_breadth"), day, decision.get("thresholds"))
        if matches != decision.get("matched"):
            raise ValueError("S05冻结分流条件与市场广度不一致")
        branch = next(iter(matches), None)
        if decision["schema"] == MARKET_ROUTE:
            code = selection.get("branch_code")
            if type(code) is not int or code not in BRANCH_CODES or BRANCH_CODES[code] != branch:
                raise ValueError("S05交易日分支参数须为与归档一致的正整数1~4")
    if "entry_branch" not in selection or selection["entry_branch"] != branch:
        raise ValueError("S05冻结分流与市场广度不一致")
    return branch


def frozen_daily_branch(selection: dict, day: date) -> str | None:
    """The daily schedule applies to common templates without opting into old entry execution."""
    if not isinstance(selection, dict) or (selection.get("entry_route") or {}).get("schema") != MARKET_ROUTE:
        raise ValueError("等待目标交易日S05整数分支参数归档")
    if selection.get("entry_execution") == "common_template_v1":
        selection = {**selection, "entry_routing": POLICY}
    return frozen_route(selection, day)


def no_market_route_receipt(selection):
    """Project a verified selection decision as a terminal zero-entry outcome."""
    return {"state": "rejected", "status": "succeeded", "terminal": True, "published": True,
            "executed": False, "count": 0, "windows": [], "completion_reason": "s05_no_market_route",
            "completed_at": selection["entry_route"]["decided_at"], "reason": NO_MARKET_ROUTE,
            **({"branch_code": 4} if selection.get("branch_code") == 4 else {})}


def project_no_market_route(result, selection):
    """An explicit pass on entry never obscures any actual prior buy rows."""
    if ("entry_route" not in selection or selection.get("entry_branch") is not None
            or result.get("rows") or result.get("entry_signal_hits")
            or (result.get("sections") or {}).get("buy")):
        return
    result["section_statuses"] = {**(result.get("section_statuses") or {}), "buy": "rejected"}
    result["section_errors"] = {k: v for k, v in (result.get("section_errors") or {}).items() if k != "buy"}
    result["entry_filter_error"] = None
    result["monitor_tasks"] = {**(result.get("monitor_tasks") or {}), "buy": no_market_route_receipt(selection)}
    result["execution_status"] = {"state": "success", "reason": None,
                                  "updated_at": int(datetime.fromisoformat(
                                      selection["entry_route"]["decided_at"]).timestamp() * 1000)}


def project_daily_buys(result, selection=None):
    """Read the numeric day's proved buys; retain the unmodified raw archive."""
    sections = result.get("sections") or {}
    if result.get("section_mode") != "signals" or "buy" not in sections:
        return result
    frozen = result.get("selection_snapshot") or {}
    if not frozen.get("entry_route"):
        frozen = selection or {}
    if (frozen.get("entry_route") or {}).get("schema") != MARKET_ROUTE:
        return result
    error = None
    try:
        branch = frozen_daily_branch(frozen, date.fromisoformat(str(result["as_of"])))
    except ValueError as exc:
        branch, error = None, str(exc)
    buys = sections["buy"]
    column = "csgi_" + ENTRY_IDS[branch] if branch else None
    hits = [hit for hit in result.get("entry_signal_hits") or [] if column in hit.get("signals", [])]
    by_symbol = {}
    for hit in hits:
        by_symbol.setdefault(hit["symbol"], []).append(hit)
    kept = []
    for row in buys:
        # Do not borrow another branch's entry price when duplicate signal hits exist.
        confirmed = row.get("entry_evaluated_at")
        if row.get(column) is not False and any(
            not (confirmed and hit.get("evaluated_at")) or confirmed == hit["evaluated_at"]
            for hit in by_symbol.get(row["symbol"], ())
        ):
            kept.append(row)
    if branch and kept == buys and all(
        hit.get("signals") == [column] for hit in result.get("entry_signal_hits") or []
    ):
        return result
    symbols = {row["symbol"] for row in kept}
    outcome = {**result, "rows": kept, "total": len(kept), "sections": {**sections, "buy": kept},
               "scores": {s: score for s, score in (result.get("scores") or {}).items() if s in symbols},
               "entry_signal_hits": [{**hit, "signals": [column]} for hit in hits if hit["symbol"] in symbols]}
    if branch is None and error is None:
        project_no_market_route(outcome, frozen)
    elif error or (buys and not kept):
        message = error or "买入归档缺少当日分支的命中证据"
        outcome.update(entry_filter_error=message, execution_status=None,
                       section_statuses={**(result.get("section_statuses") or {}), "buy": "waiting"},
                       section_errors={**(result.get("section_errors") or {}), "buy": message})
    return outcome


def entry_rows(selection):
    return [row for row in selection["rows"] if row.get("s02_rank") is not None
            and not row.get("s02_entry_exclusion")]


def evaluate_signal(frame, selection, day, signal, observed, *, unavailable=None):
    """Only the frozen route evaluates, using its saved clock and completed start bar."""
    branch = frozen_route(selection, day)
    evidence = {"entry_branch": branch, "stage": "inactive"}
    if branch is None or ENTRY_IDS[branch] != signal.get("id"):
        return [], evidence
    _, at, confirmed = branch_clock(signal, day)
    params = entry_parameters(branch, (signal.get("params") or {}).get("s05_entry", {}))
    evidence.update(bar_at=at.isoformat(), confirmed_at=confirmed.isoformat(),
                    sort_by=params["sort_by"])
    if params["sort_by"] == "fund_tech":
        evidence["fund_weight_pct"] = params["fund_weight_pct"]
    if observed is not None and as_cn_datetime(observed).replace(tzinfo=None) < confirmed:
        return [], {**evidence, "stage": "waiting"}
    unavailable = unavailable or {}
    rows = entry_rows(selection)
    usable = [row for row in rows if row["symbol"] not in unavailable]
    hits, errors, counts = select_entry(usable, frame, branch, at, selection["market_breadth"][-1],
                                        params=params, market_gate_frozen="entry_route" in selection)
    if rows and not counts["usable"]:
        raise ValueError("S02四维前50中没有可计算的股票")
    return [{**row, "entry_evaluated_at": confirmed} for row in hits], {
        **evidence, **counts, "stage": "confirmed", "invalid_symbols": {**unavailable, **errors}}


def branch_clock(signal, day):
    """Read the bar label and confirmation from the saved signal."""
    from app.strategy.late_session_strength import TEMPLATE, bar_time, evaluation_time, parameters

    branch = next((key for key, signal_id in ENTRY_IDS.items()
                   if signal.get("id") == signal_id), None)
    if branch is None or signal.get("template") != TEMPLATE:
        raise ValueError("未知的S05分流入场触发器")
    p = parameters(signal)
    if (p["time"] is None or p["end_time"] is None or signal.get("conditions")
            or any(p[key] is not None for key in (
                "pool_up_min_pct", "change_min_pct", "change_max_pct", "ma5_1m_count", "ma5_5m_count",
                "entry_groups", "amount_min_100m", "turnover_min_pct", "amplitude_max_pct", "volume_ratio_min",
            ) if key in p)):
        raise ValueError("S05入场需要启动和结束时间, 并使用专用分支条件")
    return branch, bar_time(signal, day), evaluation_time(signal, day)


def entry_parameters(branch, raw):
    defaults = {**MARKET_DEFAULTS[branch], **ENTRY_DEFAULTS[branch], "fund_weight_pct": 70}
    if not isinstance(raw, dict) or raw.keys() - defaults.keys():
        raise ValueError("S05入场包含未知参数")
    params = {**defaults, **raw}
    if params["sort_by"] not in ENTRY_SORTS:
        raise ValueError("S05入场排序指标无效")
    for key, value in params.items():
        if key == "sort_by":
            continue
        minimum = 0 if (key in ("fund_weight_pct", "position_min_pct")
                        or (key in MARKET_DEFAULTS[branch] and key.endswith("_pct"))) else -100
        if not _number(value, minimum, 100):
            raise ValueError(f"S05入场数值 {key} 须为 {minimum}~100")
    if "change_min_pct" in params and params["change_min_pct"] > params["change_max_pct"]:
        raise ValueError("S05入场涨幅下限不得大于上限")
    return params


def select_entry(rows: list[dict], frame: pl.DataFrame, branch: str, at: datetime, market: dict, *,
                 params=None, market_gate_frozen=False):
    """Return at most one hit, without promoting second place on a veto."""
    if branch not in BRANCHES:
        raise ValueError("未知S05买入分支")
    if params is None:
        params = entry_parameters(branch, {})
    sort_by = params["sort_by"]
    rows = [r for r in rows if r.get("s02_rank") is not None]
    evidence = {"branch": branch, "total": len(rows), "usable": 0, "passed": 0}
    if not rows:
        return [], {}, evidence
    if not {"symbol", "datetime", "open", "high", "low", "close"} <= set(frame.columns):
        raise ValueError("S02买入分钟缺少价格字段")
    if not _number(market.get("up"), 0, 1) or not _number(market.get("above_ma20"), 0, 1):
        raise ValueError("S02冻结市场广度无效")
    symbols = [r["symbol"] for r in rows]
    counts = Counter(symbols)
    rank_counts = Counter(r["s02_rank"] for r in rows)
    prices = ("open", "high", "low", "close")
    raw = [pl.col(f"raw_{name}").alias(name) for name in prices if f"raw_{name}" in frame.columns]
    dt = pl.col("datetime")
    if frame.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    bars = (frame.with_columns(dt, *raw)
            .filter(pl.col("symbol").is_in(symbols) & (pl.col("datetime").dt.date() == at.date())
                    & (pl.col("datetime") <= at)
                    & ((pl.col("datetime").dt.time().is_between(time(9, 31), time(11, 30)))
                       | pl.col("datetime").dt.time().is_between(time(13, 1), time(15, 0))))
            .unique(["symbol", "datetime"], keep="last", maintain_order=True)
            .sort(["symbol", "datetime"]).partition_by("symbol", as_dict=True))
    matches, errors = [], {}
    for row in rows:
        symbol = row["symbol"]
        rank, score = row["s02_rank"], row.get("s02_score")
        if (type(rank) is not int or not 1 <= rank <= 50 or not _number(score, 0, 100)
                or counts[symbol] != 1 or rank_counts[rank] != 1):
            errors[symbol] = "S02四维归档名次、分数或成员重复异常"
            continue
        prices_at = bars.get((symbol,))
        if prices_at is None or prices_at["datetime"][-1] != at:
            errors[symbol] = "缺少信号启动分钟"
            continue
        last = prices_at.row(-1, named=True)
        close, prev = last["close"], row.get("reference_close")
        if not _number(close, 1e-12) or not _number(prev, 1e-12):
            errors[symbol] = "启动分钟或冻结昨收无效"
            continue
        change = (close / prev - 1) * 100
        by_time = dict(zip(prices_at["datetime"], prices_at["close"], strict=True))

        def momentum(length, by_time=by_time, close=close):
            base = by_time.get(at - timedelta(minutes=length))
            return (close / base - 1) * 100 if _number(base, 1e-12) else None

        r5 = momentum(5) if branch == "tail" or sort_by == "return_5m" else None
        if sort_by == "return_5m" and r5 is None:
            errors[symbol] = "缺少近5分钟涨幅所需的已完成价格"
            continue
        if branch == "tail":
            r15 = momentum(15)
            if r5 is None or r15 is None:
                errors[symbol] = "缺少启动日前5或15分钟的已完成价格"
                continue
            passed = r5 >= params["return_5m_min_pct"] - EPSILON and r15 >= params["return_15m_min_pct"] - EPSILON
        elif branch == "board":
            passed = market_gate_frozen or (
                market["up"] >= params["prior_up_min_pct"] / 100 - EPSILON
                and market["above_ma20"] >= params["prior_above_ma20_min_pct"] / 100 - EPSILON)
        else:
            opening, upper, lower = prices_at["open"][0], prices_at["high"].max(), prices_at["low"].min()
            r15 = momentum(15)
            if (prices_at["datetime"][0].time() != time(9, 31) or r15 is None
                    or not all(_number(x, 1e-12) for x in (opening, upper, lower))
                    or lower > close or upper < close):
                errors[symbol] = "S05低缺口所需开盘或量价窗口不完整"
                continue
            position = (close - lower) / (upper - lower) if upper > lower else .5
            gap = (opening / prev - 1) * 100
            passed = (gap <= params["gap_max_pct"] + EPSILON
                      and position >= params["position_min_pct"] / 100 - EPSILON
                      and r15 >= params["return_15m_min_pct"] - EPSILON)
        if sort_by == "fund_tech":
            fund, tech = row.get("s02_fund_score"), row.get("s02_tech_score")
            if not all(_number(x, 0, 100) for x in (fund, tech)):
                errors[symbol] = "S05资金技术排序所需冻结分数不完整"
                continue
            priority = (params["fund_weight_pct"] * fund + (100 - params["fund_weight_pct"]) * tech) / 100
        else:
            priority = r5 if sort_by == "return_5m" else score
        evidence["usable"] += 1
        if passed and params.get("change_min_pct", -math.inf) - EPSILON <= change <= params["change_max_pct"] + EPSILON:
            matches.append((-priority, rank, symbol, {
                **last, "last_datetime": at, "change_pct": change / 100, "score": score,
                "s02_rank": rank, "entry_priority": 1, "entry_branch": branch,
                "execution_policy": POLICY, "entry_sort_by": sort_by, "entry_sort_value": priority,
                **({"entry_sort_fund_weight_pct": params["fund_weight_pct"]} if sort_by == "fund_tech" else {}),
            }))
    if not matches:
        return [], errors, evidence
    hit = min(matches, key=lambda item: item[:3])[3]
    if branch == "low_gap" and round(hit["change_pct"] * 10000) >= params["veto_change_min_pct"] * 100:
        evidence["skipped_symbol"] = hit["symbol"]
        return [], errors, evidence
    evidence["passed"] = 1
    return [hit], errors, evidence


def entry_window(start: datetime, *, signal) -> tuple[datetime, datetime]:
    """Keep the first eligible label at +2 or later, through the saved end."""
    at = as_cn_datetime(start)
    if start.tzinfo is None:
        at = at.replace(tzinfo=None)
    from app.strategy.late_session_strength import execution_end

    _, bar, confirmed = branch_clock(signal, at.date())
    if at.replace(tzinfo=None) != bar:
        raise ValueError("S05成交窗口与保存的启动分钟不匹配")
    confirmed = confirmed.replace(tzinfo=at.tzinfo)
    if confirmed.second or confirmed.microsecond:
        confirmed = confirmed.replace(second=0, microsecond=0) + timedelta(minutes=1)
    first = max(at + timedelta(minutes=2), confirmed)
    end = execution_end(signal, at.date()).replace(tzinfo=at.tzinfo)
    stop = min(end, datetime.combine(at.date(), time(14, 55), at.tzinfo))
    if at.second or at.microsecond or not time(13, 1) <= at.time() or first > stop:
        raise ValueError("S05买入窗口无有效成交分钟")
    return first, stop


def exit_window(trigger: datetime) -> tuple[datetime, datetime]:
    """Open at the next exchange minute, for ≤30 civil minutes through 14:00."""
    from app.backtest.entry_execution import expected_trading_minutes

    if trigger.second or trigger.microsecond:
        raise ValueError("S02卖出触发须为完整分钟")
    next_bar = expected_trading_minutes(trigger + timedelta(minutes=1), count=1)
    boundary = datetime.combine(trigger.date(), time(14), trigger.tzinfo)
    if not next_bar or next_bar[0] > boundary:
        raise ValueError("S02已错过14:00卖出截止")
    first = next_bar[0]
    if first.time() <= time(11, 30):
        boundary = min(boundary, datetime.combine(first.date(), time(11, 30), first.tzinfo))
    return first, min(boundary, first + timedelta(minutes=29))
