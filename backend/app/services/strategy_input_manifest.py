"""S02阶段输入契约: 固定原始值、逐股依赖证明及可重算的语义摘要。

本模块不采集、不封存文件、不写任务成功; ready只表示本阶段输入可计算。
当前规则见docs/reference/s02.md, 完整池身份与可用计算成员分开保存。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import date, datetime, time, timedelta

import polars as pl

from app.market_time import as_cn_datetime

CONTRACT = "s02_input_manifest_v1"
S01_ID = "custom_mvxxcs01"
S02_ID = "custom_mvxxcp88"
SIGNALS = {S01_ID: "late_session_strength_s01", S02_ID: "late_session_strength"}
SEMANTIC_FIELDS = ("input", "phase", "coverage", "state", "usable_symbols", "invalid_symbols")
# 传输只包含公开计算依赖; 租户业务状态、账号及真实执行时钟不混入预选行。
SELECTION_FIELDS = frozenset({
    "symbol", "name", "reference_date", "reference_close", "reference_prior_ex_date",
    "raw_close", "atr14", "atr14_previous_close", "atr_reference_date",
    "atr_rank", "atr_rank_score", "atr_rank_population", "score", "atr_metric_error",
})
STATUS_FIELDS = frozenset({
    "policy", "version", "target_date", "observed_at", "scheduled_for", "cutoff", "finished_at",
    "coverage_complete", "pending_count", "errors", "suspended", "resumed", "risk_warning",
    "risk_removed", "excluded_symbols", "removed_selection_symbols", "added_pool_symbols",
    "removed_pool_symbols", "pool_total",
})


def normalize_request(value):
    """校验S02请求身份、日期、窗口和集合, 裁掉无关业务字段。

    显式null原样保留; 只允许精确的请求字段, 预选必须无重复且属于完整池。
    返回深拷贝, 避免随后调用者修改已校验的嵌套输入。
    """
    required = {"strategy_id", "as_of", "reference_date", "bar_at", "definition_sha256",
                "algorithm_sha256", "signal", "pool", "selection", "retry_until"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("输入请求字段无效")
    if len(canonical(value)) > 4 * 1024 * 1024:
        raise ValueError("输入请求过大")
    request = deepcopy(value)
    if (any(not isinstance(request[key], dict) for key in ("pool", "selection", "signal"))
            or not isinstance(request["signal"].get("params"), dict)):
        raise ValueError("输入请求嵌套字段无效")
    for key in ("definition_sha256", "algorithm_sha256"):
        if not isinstance(request[key], str) or not re.fullmatch(r"[0-9a-f]{64}", request[key]):
            raise ValueError("输入定义摘要无效")
    if request["strategy_id"] not in SIGNALS:
        raise ValueError("输入策略身份无效")
    day = date.fromisoformat(request["as_of"])
    deadline = _wall(request["retry_until"])
    at = _wall(request["bar_at"])
    if deadline.date() != day or not at <= deadline <= datetime.combine(day, time(14, 55)):
        raise ValueError("输入重试期限无效")
    signal = request["signal"]
    if signal.get("id") != SIGNALS[request["strategy_id"]] or signal.get("conditions"):
        raise ValueError("输入信号依赖无效")
    if request["strategy_id"] == S01_ID and any(signal["params"].get(key) is not None for key in (
            "entry_groups", "turnover_min_pct", "volume_ratio_min")):
        raise ValueError("S01权威输入不支持另需股本、日均量或分组的信号")
    request["signal"] = {"id": signal["id"], "params": deepcopy(signal["params"])}
    for key in ("ma5_1m_count", "ma5_5m_count"):
        count = signal["params"].get(key)
        if count is not None and (type(count) is not int or not 1 <= count <= 120):
            raise ValueError("输入MA窗口无效")
    required_minutes(request, "stocks")
    pool = request["pool"]
    symbols = pool.get("symbols", [])
    from app.cell.context import SYMBOL_PATTERN
    if (not isinstance(symbols, list) or len(symbols) > 10000
            or any(not isinstance(s, str) or not re.fullmatch(SYMBOL_PATTERN, s) for s in symbols)):
        raise ValueError("输入完整池成员无效")
    if len(set(symbols)) != len(symbols):
        raise ValueError("输入完整池成员重复")
    request["pool"] = {key: deepcopy(pool[key]) for key in (
        "symbols", "definition_version", "as_of", "reference_date", "total",
        "suspended_count", "unknown_suspension_count") if key in pool}
    selected = request["selection"]
    request["selection"] = {key: deepcopy(selected[key]) for key in (
        "as_of", "source_date", "definition_version", "coverage") if key in selected}
    if "rows" in selected:
        if (not isinstance(selected["rows"], list) or len(selected["rows"]) > 10000
                or any(not isinstance(row, dict) or not isinstance(row.get("symbol"), str)
                       for row in selected["rows"])):
            raise ValueError("输入预选成员无效")
        rows = [{key: deepcopy(item) for key, item in row.items() if key in SELECTION_FIELDS}
                for row in selected["rows"]]
        selected_symbols = [row.get("symbol") for row in rows]
        if (len(set(selected_symbols)) != len(rows) or not set(selected_symbols) <= set(symbols)):
            raise ValueError("输入预选成员重复或不属于完整池")
        request["selection"]["rows"] = rows
    for source, target in ((pool, request["pool"]), (selected, request["selection"])):
        for key in ("status_refresh", "status_evidence"):
            if key in source:
                target[key] = {field: deepcopy(item) for field, item in source[key].items()
                               if field in STATUS_FIELDS}
    return request


def canonical(value):
    """按原数值精度稳定编码; 拒绝NaN/无穷, 日期统一ISO, 不做价格舍入。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False, default=str).encode()


def digest(value):
    """对同一稳定编码求SHA-256; 摘要范围由调用者明确选择。"""
    return hashlib.sha256(canonical(value)).hexdigest()


def _wall(value):
    """转为北京时间墙钟, 统一有时区与无时区数据的窗口比较。"""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return as_cn_datetime(parsed).replace(tzinfo=None)


def required_minutes(request, phase):
    """按已启用条件解析固定启动分钟的全部依赖, 不以观察时钟推进右端点。

    广度只读启动分钟; N个MA5需N+4根对应周期K, 5m先要求完整基础1m。
    当前7/4配置的并集为13:51~14:30; 累计条件另按完成交易分钟声明依赖。
    """
    day, at = date.fromisoformat(request["as_of"]), _wall(request["bar_at"])
    if at.date() != day or at.second or at.microsecond:
        raise ValueError("输入目标分钟日期不一致")
    if request["reference_date"] >= request["as_of"]:
        raise ValueError("输入参考日期无效")
    if phase not in ("breadth", "stocks"):
        raise ValueError("输入阶段无效")
    params = request["signal"]["params"]
    if params.get("time") and at.time() != time.fromisoformat(params["time"]):
        raise ValueError("输入目标分钟与保存信号不一致")
    if phase == "breadth":
        return [at]
    needed = {at}
    for key, period in (("ma5_1m_count", 1), ("ma5_5m_count", 5)):
        count = params.get(key)
        if count is None:
            continue
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ValueError("输入MA窗口无效")
        end = at.replace(minute=at.minute // period * period)
        needed.update(end - timedelta(minutes=i) for i in range((count + 4) * period))
    if request["strategy_id"] != S01_ID and any(params.get(key) is not None for key in
           ("amount_min_100m", "turnover_min_pct", "amplitude_max_pct", "volume_ratio_min")):
        # 累计条件只声明交易时段分钟; 本函数不生成行情, 实际行随后去重和校验。
        opening = datetime.combine(day, time(9, 31))
        needed.update(opening + timedelta(minutes=i) for i in range(
            int((at - opening).total_seconds() // 60) + 1)
            if (opening + timedelta(minutes=i)).time() <= time(11, 30)
            or (opening + timedelta(minutes=i)).time() >= time(13, 1))
    if any(value.date() != day for value in needed):
        raise ValueError("输入窗口跨越目标日期")
    return sorted(needed)


def minute_bounds(request, phase):
    """S01个股读取全部可用交易分钟前缀, 只将启动和启用的MA窗视为必需。"""
    required = required_minutes(request, phase)
    start = (required[-1].replace(hour=9, minute=31)
             if phase == "stocks" and request["strategy_id"] == S01_ID else required[0])
    return start, required[-1]


def common_identity(request):
    """提取两阶段共用的定义/算法身份; 实际池、预选及数据由阶段语义摘要覆盖。"""
    identity = {key: deepcopy(request[key]) for key in (
        "strategy_id", "as_of", "reference_date", "bar_at",
        "definition_sha256", "algorithm_sha256", "signal")}
    identity["signal"] = {key: deepcopy(request["signal"][key]) for key in ("id", "params")}
    return identity


def common_key(request):
    """生成两阶段共同的存储键; 阶段名再区分各自不可变文件。"""
    return digest(common_identity(request))


def _proven_missing(proof, required, *, symbol, dependency, day):
    """至少两个不同正请求序号的成功完整响应, 才证明单股当日窗口仍缺数据。

    响应须明确股票、日期及完整范围; 采集完成时间或重复同一序号均不足为证。
    证明结构缺失或无效时返回False, 使缺口继续等待。
    """
    try:
        receipts = proof.get(dependency, [])
        sequences = {row["request_seq"] for row in receipts
                     if row.get("state") == "succeeded" and row.get("symbol") == symbol
                     and row.get("day") == day and type(row.get("request_seq")) is int
                     and row["request_seq"] > 0
                     and _wall(row["required_from"]) <= required[0]
                     and _wall(row["completed_through"]) >= required[-1]}
        return len(sequences) >= 2
    except (KeyError, TypeError, ValueError):
        return False


def _number(value, *, positive=False):
    """数值校验排除布尔及非有限值; 价格须正, 量额和版本允许零。"""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (value > 0 if positive else value >= 0))


def _safe(value):
    """递归规范化JSON值; 非有限原值转null后仍由覆盖校验标为非法。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe(item) for item in value]
    return value


def build_manifest(request, *, phase, minutes, references, reference_errors,
                   coverage, source_evidence, published_at, timing=None):
    """构造单阶段清单, 区分available、pending、invalid及unavailable。

    缺行必须有成功完整响应证明才可隔离, 普通采集墙钟不能使输入ready。
    有pending就等待; 成员非空却全不可用为失败; 明确成功空预选可ready。
    完整池仍保存全量身份, usable_symbols仅限定Compute计算成员。
    """
    required = required_minutes(request, phase)
    pool = sorted(set(request["pool"]["symbols"]))
    selected = request["selection"]
    if (selected.get("as_of") != request["as_of"]
            or selected.get("source_date") != request["reference_date"]
            or not isinstance(selected.get("rows"), list)):
        raise ValueError("输入缺少精确日期冻结预选")
    members = pool if phase == "breadth" else sorted({r["symbol"] for r in selected["rows"]})
    if not set(members) <= set(pool):
        raise ValueError("冻结资格不属于完整池")
    columns = {"symbol", "datetime", "close"}
    params = request["signal"]["params"]
    available_prefix = phase == "stocks" and request["strategy_id"] == S01_ID
    if phase == "stocks":
        if available_prefix:
            # S01源码投影还使用开盘价、原始高低价和成交额计算原始分数。
            columns.update(("open", "high", "low", "amount"))
        if params.get("amount_min_100m") is not None:
            columns.add("amount")
        if params.get("turnover_min_pct") is not None or params.get("volume_ratio_min") is not None:
            columns.add("volume")
        if params.get("amplitude_max_pct") is not None:
            columns.update(("high", "low"))
    frame = minutes
    if frame.is_empty():
        frame = pl.DataFrame(schema={"symbol": pl.String, "datetime": pl.Datetime, "close": pl.Float64})
    if not {"symbol", "datetime", "close"} <= set(frame.columns):
        raise ValueError("分钟输入清单缺少必要列")
    dt = pl.col("datetime")
    if frame.schema["datetime"].time_zone:
        dt = dt.dt.convert_time_zone("Asia/Shanghai").dt.replace_time_zone(None)
    visible = pl.col("datetime").is_in(required)
    if available_prefix:
        start, end = minute_bounds(request, phase)
        stamp = pl.col("datetime")
        visible |= (stamp.is_between(start, end)
                    & (stamp.dt.time().is_between(time(9, 31), time(11, 30))
                       | stamp.dt.time().is_between(time(13, 1), time(15))))
    frame = frame.with_columns(dt).filter(pl.col("symbol").is_in(members) & visible)
    from app.services.minute_write_order import VERSION_COLUMN
    if VERSION_COLUMN in frame.columns:
        # 跨进程请求序号决定同股同分钟的最后版本, 不以传输到达顺序覆盖新数据。
        if frame[VERSION_COLUMN].is_null().any() or any(
                type(value) is not int or value < 0 for value in frame[VERSION_COLUMN]):
            raise ValueError("分钟请求顺序无效")
        frame = frame.sort("symbol", "datetime", VERSION_COLUMN, maintain_order=True)
        columns.add(VERSION_COLUMN)
    frame = frame.unique(["symbol", "datetime"], keep="last", maintain_order=True).sort("symbol", "datetime")
    projection = columns | ({"volume"} if available_prefix else set())
    rows = frame.select(sorted(projection & set(frame.columns))).to_dicts()
    by_symbol = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], {})[row["datetime"]] = row
    states, usable, invalid = {}, [], {}
    selection_refs = {r["symbol"]: r.get("reference_close") for r in selected["rows"]}
    for symbol in members:
        values = by_symbol.get(symbol, {})
        missing = [stamp.isoformat() for stamp in required if stamp not in values]
        bad = reference_errors.get(symbol)
        if phase == "breadth" and symbol in source_evidence.get("source_errors", {}):
            # 广度依赖原始昨收; 个股ATR重算错误不能直接作为广度昨收错误。
            bad = source_evidence.get("daily_reference_errors", {}).get(symbol)
        ref = references.get(symbol) if phase == "breadth" else selection_refs.get(symbol)
        if ref is not None and not _number(ref, positive=True):
            bad = bad or "精确上一交易日原始收盘价无效"
        if any(not _number(row.get(field), positive=field in ("open", "close", "high", "low"))
               for row in values.values() for field in columns - {"symbol", "datetime"}):
            bad = bad or "必需分钟价格或成交字段无效"
        if "high" in columns and any(
                not row["low"] <= row["close"] <= row["high"] for row in values.values()
                if all(_number(row.get(field), positive=True) for field in ("low", "close", "high"))):
            bad = bad or "必需分钟高低价不一致"
        if not columns <= set(frame.columns) and members:
            bad = bad or "启用条件缺少必需分钟字段"
        proof = coverage.get(symbol, {})
        missing_daily = source_evidence.get("dependencies", {}).get(symbol, {}).get("missing_daily_dates", [])
        if bad:
            state, reason = "invalid", bad
        elif missing or ref is None or missing_daily:
            minute_missing = not missing or _proven_missing(
                proof, required, symbol=symbol, dependency="minute", day=request["as_of"])
            ref_at = datetime.combine(date.fromisoformat(request["reference_date"]), time.min)
            ref_missing = ref is not None or _proven_missing(
                proof, [ref_at, ref_at.replace(hour=23, minute=59, second=59)],
                symbol=symbol, dependency="daily", day=request["reference_date"])
            source_missing = all(_proven_missing(
                proof, [datetime.combine(date.fromisoformat(day), time.min),
                        datetime.combine(date.fromisoformat(day), time.max)],
                symbol=symbol, dependency="daily", day=day) for day in missing_daily)
            state = "unavailable" if minute_missing and ref_missing and source_missing else "pending"
            reason = "完整请求后仍缺少必需分钟或昨收" if state == "unavailable" else "等待必需分钟或昨收"
        else:
            state, reason = "available", None
            usable.append(symbol)
        states[symbol] = {"state": state, "missing_minutes": missing,
                          "missing_daily_dates": missing_daily,
                          "reason": reason, "proof": deepcopy(proof),
                          "reference_error": bad,
                          "missing_reference": ref is None}
        if state in ("invalid", "unavailable"):
            invalid[symbol] = deepcopy(states[symbol])
    # 任一待补成员都不能提前固化阶段; 已证实不可用成员与普通缺口分别保留。
    state = ("waiting_input" if any(row["state"] == "pending" for row in states.values())
             else "no_valid_input" if members and not usable else "ready")
    frozen = deepcopy(request)
    frozen["pool"] = {**frozen["pool"], "symbols": pool}
    frozen["selection"] = {key: deepcopy(item) for key, item in selected.items() if key != "rows"}
    frozen["selection"]["rows"] = sorted([
        {key: deepcopy(value) for key, value in row.items() if key in SELECTION_FIELDS}
        for row in selected["rows"]], key=lambda row: row["symbol"])
    frozen["minutes"] = [{**row, "datetime": row["datetime"].isoformat()} for row in rows]
    frozen["references"] = {symbol: references.get(symbol) for symbol in pool}
    frozen["source_evidence"] = deepcopy(source_evidence)
    result = _safe({"contract": CONTRACT, "input": frozen, "phase": phase, "coverage": states,
              "state": state, "usable_symbols": usable, "invalid_symbols": invalid,
              "published_at": published_at})
    result["input_sha256"] = digest({key: result[key] for key in SEMANTIC_FIELDS})
    if timing:
        result["timing"] = _safe(timing)
    return result


def validate_manifest(value):
    """接收/绑定前重算原始证据、昨收映射、覆盖及摘要, 拒绝只改标签的清单。

    传输SHA正确仍须通过此语义校验; 重建使用相同输入值和证明, 不访问市场。
    """
    if value.get("contract") != CONTRACT:
        raise ValueError("输入清单契约无效")
    try:
        actual = digest({key: value[key] for key in SEMANTIC_FIELDS})
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("输入清单摘要无效") from exc
    if value.get("input_sha256") != actual:
        raise ValueError("输入清单摘要不一致")
    required_minutes(value["input"], value["phase"])
    frozen = value["input"]
    from app.services.strategy_input_source import validate_evidence
    try:
        source_errors = validate_evidence(frozen, frozen["source_evidence"])
        references = pl.DataFrame(frozen["source_evidence"]["reference_daily"], infer_schema_length=None)
        daily_errors = {}
        actual_references = {}
        if not references.is_empty():
            from app.services.daily_close_validation import require_final_daily_rows
            references = references.with_columns(pl.col("date").str.to_date())
            daily_errors = require_final_daily_rows(
                references, date.fromisoformat(frozen["reference_date"]), isolate_symbols=True) or {}
            valid = references.filter(~pl.col("symbol").is_in(list(daily_errors)))
            actual_references = dict(zip(valid["symbol"], valid["close"], strict=True))
        if ({symbol: actual_references.get(symbol) for symbol in frozen["pool"]["symbols"]}
                != frozen["references"]):
            raise ValueError("原始昨收与冻结参考不一致")
        errors = {**source_errors, **daily_errors}
        if errors != frozen["source_evidence"].get("reference_errors", {}):
            raise ValueError("原始日线异常与冻结证据不一致")
    except (KeyError, TypeError, pl.exceptions.PolarsError) as exc:
        raise ValueError("输入缺少可重算的原始日线和除权证据") from exc
    frame = pl.DataFrame(frozen["minutes"], infer_schema_length=None)
    if not frame.is_empty():
        frame = frame.with_columns(pl.col("datetime").str.to_datetime())
    rebuilt = build_manifest(
        frozen, phase=value["phase"], minutes=frame, references=frozen["references"],
        reference_errors=errors if value["phase"] == "stocks" else daily_errors,
        coverage={symbol: row["proof"] for symbol, row in value["coverage"].items()},
        source_evidence=frozen["source_evidence"], published_at=value["published_at"])
    if any(rebuilt[key] != value[key] for key in SEMANTIC_FIELDS):
        raise ValueError("输入清单覆盖与实际数据不一致")
    return actual


def bind_manifest(value, request, phase):
    """校验清单属于所请求阶段/定义且ready, 返回隔离的冻结输入副本。"""
    validate_manifest(value)
    if value["phase"] != phase or common_key(value["input"]) != common_key(request):
        raise ValueError("输入清单定义或计算身份不一致")
    if value["state"] != "ready":
        raise ValueError("输入清单尚未就绪")
    return deepcopy(value["input"])
