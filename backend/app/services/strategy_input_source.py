"""从原始日线和带日期的除权因子核验S02冻结参考, 不在盘中重排资格。

已知ATR须可重算, 未知冻结ATR保留未知; 逐股错误与整份证据错误分开。
当前规则见docs/reference/s02.md。
"""
from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import polars as pl

from app.services.strategy_input_manifest import S01_ID, _number, _safe, digest


def expected_dates(request):
    """解析精确T-1及其前14个交易日, 拒绝用最近有行情日代替。"""
    from app.strategy.basic_filter import previous_session
    days = [date.fromisoformat(request["reference_date"])]
    if previous_session(date.fromisoformat(request["as_of"])) != days[0]:
        raise ValueError("输入不是精确上一交易日")
    for _ in range(0 if request["strategy_id"] == S01_ID else 14):
        days.insert(0, previous_session(days[0]))
    return days


def verify_ranks(selected):
    """核验指标/代码顺序、总体及排名分, 允许状态剔除留下的顺位空档。"""
    known = [row for row in selected if _number(row.get("atr14_previous_close"))]
    if not known:
        return
    populations = {row.get("atr_rank_population") for row in known}
    if (len(populations) != 1 or any(type(value) is not int or value < len(known)
                                    for value in populations)):
        raise ValueError("冻结ATR排名总体无效")
    population = populations.pop()
    ranks = [row.get("atr_rank") for row in known]
    if (any(type(rank) is not int or not 1 <= rank <= population for rank in ranks)
            or len(set(ranks)) != len(ranks)):
        raise ValueError("冻结ATR排名重复或越界")
    ordered = sorted(known, key=lambda row: (-row["atr14_previous_close"], row["symbol"]))
    if [row["atr_rank"] for row in ordered] != sorted(ranks):
        raise ValueError("冻结ATR排名与指标及代码顺序不一致")
    for row in known:
        expected = 100. * (population - row["atr_rank"]) / (population - 1) if population > 1 else 50.
        if (not _number(row.get("atr_rank_score"))
                or not math.isclose(row["atr_rank_score"], expected, abs_tol=1e-10, rel_tol=0)):
            raise ValueError("冻结ATR排名分数不一致")


def _date_column(frame, column):
    """统一日期类型; 坏日期转null, 随后按可定位股票记错。"""
    expression = pl.col(column)
    if frame.schema[column] == pl.String:
        expression = expression.str.to_date(strict=False)
    else:
        expression = expression.cast(pl.Date, strict=False)
    return frame.with_columns(expression)


def verify_source(request, raw, factors):
    """重算已知ATR并比对冻结昨收; 返回原始证据、待补依赖和逐股错误。

    缺字段、无法定位股票等整份错误抛出; 单股坏价格/日线/因子记录后继续。
    排名属于整体顺序契约, 损坏排名不能被伪装成单股行情缺失。
    """
    from app.factors.atr import exact_atr14_sma_reference
    from app.indicators.pipeline import _apply_adj_factor
    from app.services.daily_close_validation import require_final_daily_rows

    days = expected_dates(request)
    selected = request["selection"]["rows"]
    s01 = request["strategy_id"] == S01_ID
    if not s01:
        verify_ranks(selected)
    symbols = sorted({row["symbol"] for row in selected})
    if not {"symbol", "date", "open", "high", "low", "close"} <= set(raw.columns):
        raise ValueError("原始日线证据缺少必要字段")
    if raw["symbol"].is_null().any():
        raise ValueError("原始日线证据无法定位到股票")
    raw = _date_column(raw, "date")
    raw = raw.filter(pl.col("symbol").is_in(symbols)
                     & (pl.col("date").is_in(days) | pl.col("date").is_null())).sort("symbol", "date")
    if not {"symbol", "trade_date", "ex_factor"} <= set(factors.columns):
        raise ValueError("原始除权证据缺少必要字段")
    if factors["symbol"].is_null().any():
        raise ValueError("原始除权证据无法定位到股票")
    factors = _date_column(factors, "trade_date")
    factors = factors.filter(pl.col("symbol").is_in(symbols)
                             & ((pl.col("trade_date") <= days[-1]) | pl.col("trade_date").is_null())
                             ).select("symbol", "trade_date", "ex_factor").sort("symbol", "trade_date")
    errors, metrics = {}, {}
    bad_factor = factors.filter(
        pl.col("trade_date").is_null()
        | ~pl.col("ex_factor").cast(pl.Float64, strict=False).is_finite().fill_null(False)
        | (pl.col("ex_factor").cast(pl.Float64, strict=False) <= 0).fill_null(True))
    for symbol in bad_factor["symbol"].unique():
        errors[symbol] = "原始除权因子日期或数值无效"
    if factors.schema["ex_factor"] == pl.Boolean:
        for symbol in factors["symbol"].unique():
            errors[symbol] = "原始除权因子日期或数值无效"
    conflicts = factors.group_by("symbol", "trade_date").agg(pl.col("ex_factor").n_unique().alias("values"))
    for symbol in conflicts.filter(pl.col("values") > 1)["symbol"].unique():
        errors[symbol] = "原始除权因子同日冲突"
    usable_factors = factors.filter(~pl.col("symbol").is_in(list(errors)))
    groups = raw.partition_by("symbol", as_dict=True)
    daily_errors = {}
    for day in days:
        part = raw.filter(pl.col("date") == day)
        if not part.is_empty():
            daily_errors.update(require_final_daily_rows(part, day, isolate_symbols=True) or {})
    dependencies = {}
    for row in selected:
        symbol = row["symbol"]
        frame = groups.get((symbol,))
        known = not s01 and row.get("atr14_previous_close") is not None
        needed = days if known else days[-1:]
        present = set(frame["date"].to_list()) if frame is not None else set()
        missing = [str(day) for day in needed if day not in present]
        dependencies[symbol] = {"missing_daily_dates": missing}
        try:
            if symbol in daily_errors:
                raise ValueError(daily_errors[symbol])
            if frame is not None:
                if frame["date"].is_null().any():
                    raise ValueError("原始日线日期无效")
                if any(not _number(value, positive=True) for key in ("open", "high", "low", "close")
                       for value in frame[key]):
                    raise ValueError("原始日线价格无效")
                if any(not item["low"] <= min(item["open"], item["close"])
                       <= max(item["open"], item["close"]) <= item["high"]
                       for item in frame.to_dicts()):
                    raise ValueError("原始日线高低价不一致")
                last = frame.filter(pl.col("date") == days[-1])
                if not last.is_empty() and row.get("reference_close") != last["close"][0]:
                    raise ValueError("冻结未复权昨收与原始日线不一致")
        except ValueError as exc:
            errors[symbol] = str(exc)
        if s01:
            continue
        # 冻结未知ATR只核验必要原始昨收, 不以盘中补数改成已知排名。
        if not known:
            metrics[symbol] = {"state": "unknown_frozen_metric", "reason": row.get("atr_metric_error")}
            continue
        if symbol in errors:
            metrics[symbol] = {"state": "invalid", "reason": errors[symbol]}
            continue
        if missing:
            metrics[symbol] = {"state": "pending", "missing_daily_dates": missing}
            continue
        try:
            adjusted = _apply_adj_factor(
                frame, usable_factors.filter(pl.col("symbol") == symbol)).with_columns(
                    frame["close"].alias("raw_close"))
            computed = exact_atr14_sma_reference(adjusted, [symbol], days).row(0, named=True)
            if any(not isinstance(row.get(field), (int, float))
                   or isinstance(row[field], bool) or not math.isfinite(row[field])
                   or not math.isclose(row[field], computed[field], abs_tol=1e-10, rel_tol=0)
                   for field in ("atr14", "atr14_previous_close")):
                raise ValueError("冻结ATR与原始日线及除权证据重算不一致")
            if row.get("atr_reference_date") != str(days[-1]):
                raise ValueError("冻结ATR参考日期不一致")
            metrics[symbol] = {"state": "verified", **computed}
        except ValueError as exc:
            errors[symbol] = str(exc)
            metrics[symbol] = {"state": "invalid", "reason": str(exc)}
    raw_rows, factor_rows = _safe(raw.to_dicts()), _safe(factors.to_dicts())
    evidence = _safe({
        "expected_dates": [str(day) for day in days], "raw_daily": raw_rows,
        "adjustments": factor_rows, "atr_validation": metrics, "dependencies": dependencies,
        "source_errors": errors,
        "raw_daily_sha256": digest(raw_rows), "adjustments_sha256": digest(factor_rows),
    })
    return evidence, errors


def validate_evidence(request, evidence):
    """接收端用包内原始值重建全部证据, 逐股错误和待补缺口也须一致。"""
    raw = pl.DataFrame(evidence["raw_daily"], schema_overrides={"symbol": pl.String, "date": pl.String},
                       infer_schema_length=None) if evidence["raw_daily"] else pl.DataFrame(schema={
        "symbol": pl.String, "date": pl.Date, "open": pl.Float64, "high": pl.Float64,
        "low": pl.Float64, "close": pl.Float64})
    factors = pl.DataFrame(evidence["adjustments"], infer_schema_length=None) if evidence["adjustments"] else (
        pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}))
    rebuilt, errors = verify_source(request, raw, factors)
    if any(evidence.get(key) != value for key, value in rebuilt.items()):
        raise ValueError("原始日线或除权证据重算不一致")
    return errors


def load_source(repo, request):
    """从绑定公共发布读精确15日日线及截至参考日因子, 返回核验结果。"""
    days = expected_dates(request)
    root = Path(repo.store.data_dir)
    symbols = sorted({row["symbol"] for row in request["selection"]["rows"]})
    parts = []
    for day in days:
        for path in sorted((root / "kline_daily" / f"date={day}").glob("*.parquet")):
            schema = pl.read_parquet_schema(path)
            columns = [key for key in ("symbol", "date", "open", "high", "low", "close",
                                        "quote_ts", "quote_time_basis") if key in schema]
            frame = pl.read_parquet(path, columns=columns)
            if "symbol" not in frame.columns:
                raise ValueError("原始日线证据无法定位到股票")
            parts.append(frame.filter(pl.col("symbol").is_in(symbols)))
    raw = pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema={
        "symbol": pl.String, "date": pl.Date, "open": pl.Float64,
        "high": pl.Float64, "low": pl.Float64, "close": pl.Float64})
    paths = [] if request["strategy_id"] == S01_ID else sorted((root / "adj_factor").rglob("*.parquet"))
    factors = pl.concat([pl.read_parquet(path) for path in paths], how="diagonal_relaxed") if paths else (
        pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}))
    return verify_source(request, raw, factors)
