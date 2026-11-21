"""S03 ranks an immutable S02 candidate archive; no second scope/numeric filter."""
# ruff: noqa: RUF001 -- Chinese UI messages and fullwidth announcement punctuation.
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, timedelta
from statistics import mean

import polars as pl

from app.market_time import as_cn_datetime
from app.services.selection_clock import S02_ID

POLICY = "s03_five_dimensions_v1"
WEIGHTS = {"tech_score": .4, "fund_score": .2, "emotion_score": .2,
           "fundamental_score": .1, "news_score": .1}
POSITIVE = r"中标|签订.*合同|业绩预增|扭亏|回购.*进展|回购股份.*实施|增持.*完成"
NEGATIVE = r"立案|退市|风险警示|减持|亏损|下修|处罚|违约|无法偿还|诉讼|仲裁"


def scale(value, low, high):
    if value is None or not math.isfinite(float(value)):
        return None
    return min(100., max(0., (float(value) - low) / (high - low) * 100))


def composite_score(technical, funds, sentiment, fundamental, news):
    return sum(value * weight for value, weight in zip(
        (technical, funds, sentiment, fundamental, news), WEIGHTS.values(), strict=True))


def freeze_source(snapshot, source, target):
    from app.services.daily_selections import valid_selection
    if not valid_selection(snapshot, target):
        raise ValueError("等待S02的20:00成功预选归档")
    if snapshot.get("source_date") != str(source) or not snapshot.get("identity"):
        raise ValueError("S02预选源日期或身份不匹配")
    try:
        scheduled = as_cn_datetime(datetime.fromisoformat(snapshot["scheduled_for"]))
        generated = as_cn_datetime(datetime.fromisoformat(snapshot["generated_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("S02预选缺少可验证的计划与生成时点") from exc
    if (scheduled.date() != source or scheduled.time().isoformat() != "20:00:00"
            or not scheduled <= generated <= scheduled + timedelta(hours=1)):
        raise ValueError("S03仅使用S02在20:00～21:00完成的名单，晚补归档不能替代")
    frozen = json.loads(json.dumps(snapshot, ensure_ascii=False, default=str))
    encoded = json.dumps(frozen, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {"strategy_id": S02_ID, "snapshot": frozen,
            "sha256": hashlib.sha256(encoded.encode()).hexdigest()}


def ranked_result(rows, invalid, total):
    if total and not rows:
        reasons = "; ".join(sorted(set(invalid.values())))
        raise ValueError(f"S03有{total}只S02候选但全部无法评分: {reasons}")
    ordered = sorted(rows, key=lambda row: (-row["score"], row["symbol"]))
    return {"rows": [{**row, "selection_rank": i + 1} for i, row in enumerate(ordered[:30])],
            "scoring_policy": POLICY, "scoring_weights": WEIGHTS,
            "coverage": {"schema": POLICY, "candidate_total": total,
                         "scorable_total": len(rows), "selected_total": min(len(rows), 30),
                         "invalid_symbols": invalid, "complete": not invalid},
            "warnings": [f"S03隔离{len(invalid)}只评分数据异常股票，完整股票池保留"] if invalid else []}


def _day(value):
    return date.fromisoformat(str(value)[:10]) if value is not None else None


def _number(row, key, reason):
    value = row.get(key)
    if value is None or not math.isfinite(float(value)):
        raise ValueError(reason)
    return float(value)


def _name(value):
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).upper()
    return re.sub(r"^\*?ST", "", value)


def _latest(frame, symbol, source):
    if frame is None or frame.is_empty():
        raise ValueError("缺少已观察财务数据")
    records = [row for row in frame.to_dicts() if row.get("symbol") == symbol
               and _day(row.get("announce_date")) is not None
               and _day(row["announce_date"]) < source
               and _day(row.get("period_end")) is not None
               and _day(row["period_end"]) <= source]
    if not records:
        raise ValueError("缺少T日前已公告财务数据")
    return max(records, key=lambda row: (_day(row["period_end"]), _day(row["announce_date"])))


def _fundamental(financial, symbol, source, close):
    metrics = _latest(financial.get("metrics"), symbol, source)
    income = _latest(financial.get("income"), symbol, source)
    cash = _latest(financial.get("cash_flow"), symbol, source)
    if len({_day(row["period_end"]) for row in (metrics, income, cash)}) != 1:
        raise ValueError("财务报告期不匹配")
    growth = _number(metrics, "net_income_yoy", "缺净利润增长")
    roe = _number(metrics, "roe", "缺ROE")
    debt = _number(metrics, "debt_to_asset_ratio", "缺负债率")
    eps = _number(metrics, "eps_basic", "缺每股收益")
    profit = _number(income, "net_income", "缺净利润")
    ocf = _number(cash, "net_operating_cash_flow", "缺经营现金流")
    quality = ocf / profit if profit > 0 else None
    pe = close / (eps * 12 / _day(metrics["period_end"]).month) if eps > 0 else None
    score = (.3 * scale(growth, -20, 40) + .25 * scale(roe, 0, 15)
             + .25 * (scale(quality, 0, 1.5) if quality is not None else 0)
             + .1 * (100 - scale(debt, 30, 85))
             + .1 * (100 - scale(pe, 15, 60) if pe is not None else 0))
    return score, {"growth": growth, "roe": roe, "ocf_quality": quality, "debt": debt,
                   "pe_annualized": pe, "financial_period": str(metrics["period_end"]),
                   "financial_announce": str(metrics["announce_date"])}


def rank_candidates(snapshot, daily, financial, flows, market, notices, source):
    candidates = snapshot["rows"]
    if not candidates:
        return ranked_result([], {}, 0)
    symbols = [row["symbol"] for row in candidates]
    if len(set(symbols)) != len(symbols):
        raise ValueError("S02候选归档存在重复股票")
    history = {key[0]: frame.to_dicts() for key, frame in
               daily.filter(pl.col("symbol").is_in(symbols)).sort("date").partition_by(
                   "symbol", as_dict=True).items()} if not daily.is_empty() else {}
    financial_groups = {table: {key[0]: frame for key, frame in
                               data.filter(pl.col("symbol").is_in(symbols)).partition_by(
                                   "symbol", as_dict=True).items()}
                        for table, data in financial.items() if data is not None and not data.is_empty()}
    notice_groups = {key[0]: frame.to_dicts() for key, frame in
                     notices.filter(pl.col("symbol").is_in(symbols)).partition_by(
                         "symbol", as_dict=True).items()} if not notices.is_empty() else {}
    flow_map = {}
    duplicate_flows = set()
    for row in flows.filter(pl.col("symbol").is_in(symbols)).to_dicts() if not flows.is_empty() else []:
        try:
            key = (row["symbol"], _day(row["date"]))
        except (ValueError, TypeError):
            duplicate_flows.add(row["symbol"])
            continue
        if key in flow_map:
            duplicate_flows.add(row["symbol"])
        flow_map[key] = row
    ranked, invalid = [], {}
    for candidate in candidates:
        symbol = candidate["symbol"]
        try:
            records = [row for row in history.get(symbol, []) if _day(row["date"]) <= source][-65:]
            if (len(records) < 60 or _day(records[-1]["date"]) != source
                    or len({_day(row["date"]) for row in records}) != len(records)):
                raise ValueError("缺完整60日日线或日线重复")
            closes = [_number(row, "close", "日线价格缺失") for row in records]
            if min(closes) <= 0:
                raise ValueError("日线价格非正")
            current, close = records[-1], closes[-1]
            ma5, ma10, ma20, ma60 = [mean(closes[-n:]) for n in (5, 10, 20, 60)]
            slope = ma20 / mean(closes[-25:-5]) - 1
            momentum, bias = close / closes[-21] - 1, close / ma20 - 1
            ema12 = ema26 = closes[0]
            dea = 0.
            for price in closes:
                ema12 += (price - ema12) * 2 / 13
                ema26 += (price - ema26) * 2 / 27
                dif = ema12 - ema26
                dea += (dif - dea) * 2 / 10
            hist = (dif - dea) / close
            stack = 25 * sum((close > ma5, ma5 > ma10, ma10 > ma20, ma20 > ma60))
            tech = (.3 * stack + .2 * scale(slope, -.01, .03)
                    + .2 * scale(momentum, -.05, .20) + .15 * scale(hist, -.003, .006)
                    + .15 * (100 - scale(abs(bias), .04, .18)))
            if symbol in duplicate_flows:
                raise ValueError("资金日线存在重复")
            ratios = []
            for row in records[-20:]:
                flow = flow_map.get((symbol, _day(row["date"])), {})
                amount = _number(row, "amount", "缺资金分母成交额")
                if amount <= 0:
                    raise ValueError("资金分母成交额非正")
                ratios.append((_number(flow, "super_large_net_inflow", "缺资金20日超大单净流入")
                               + _number(flow, "large_net_inflow", "缺资金20日大单净流入")) / amount)
            flow5, flow20 = mean(ratios[-5:]), mean(ratios)
            positive5 = sum(value > 0 for value in ratios[-5:]) / 5
            funds = (.35 * scale(ratios[-1], -.10, .15) + .3 * scale(flow5, -.05, .10)
                     + .2 * scale(flow20, -.03, .06) + .15 * scale(positive5, .2, .8))
            high, low = [_number(current, key, "缺当日高低价") for key in ("high", "low")]
            if high < low or not low <= close <= high:
                raise ValueError("当日高低价与收盘价不一致")
            position = (close - low) / (high - low) if high > low else .5
            amount20 = mean(_number(row, "amount", "缺20日成交额") for row in records[-20:])
            if amount20 <= 0:
                raise ValueError("20日平均成交额非正")
            amount_ratio = current["amount"] / amount20
            change = close / closes[-2] - 1
            breadth = _number(market, "breadth", "缺市场上涨广度")
            emotion = (.25 * scale(change, 0, .06) + .3 * position * 100
                       + .25 * scale(amount_ratio, .7, 2) + .2 * scale(breadth, 0, 1))
            fundamental, fin_inputs = _fundamental(
                {table: groups.get(symbol) for table, groups in financial_groups.items()},
                symbol, source, _number(current, "raw_close", "缺原始收盘价"))
            positive = negative = 0
            used = []
            if not _name(current.get("name")):
                raise ValueError("缺证券简称，无法匹配公告发行人")
            seen_notices = set()
            for notice in notice_groups.get(symbol, []):
                stamp = _day(notice.get("display_time") or notice.get("date"))
                title = str(notice.get("title") or "")
                issuer = re.split("[:：]", title, maxsplit=1)[0]
                if (notice.get("symbol") != symbol or stamp is None
                        or not source - timedelta(days=10) <= stamp < source
                        or not _name(current.get("name")) or _name(issuer) != _name(current["name"])):
                    continue
                notice_id = notice.get("announcement_id") or title
                if notice_id in seen_notices:
                    continue
                seen_notices.add(notice_id)
                positive += bool(re.search(POSITIVE, title))
                negative += bool(re.search(NEGATIVE, title))
                used.append(notice_id)
            news = min(100., max(0., 50 + min(positive, 2) * 10 - min(negative, 2) * 25))
            scores = dict(zip(WEIGHTS, (tech, funds, emotion, fundamental, news), strict=True))
            ranked.append({**candidate, **scores, "score": composite_score(*scores.values()),
                           "scoring_inputs": {**fin_inputs, "ma5": ma5, "ma10": ma10,
                               "ma20": ma20, "ma60": ma60, "ma20_change5": slope,
                               "momentum20": momentum, "macd_hist_pct": hist, "bias20": bias,
                               "flow1": ratios[-1], "flow5": flow5, "flow20": flow20,
                               "flow_positive5": positive5, "return_t": change,
                               "close_position": position, "amount_ratio20": amount_ratio,
                               "breadth": breadth, "news_positive": positive,
                               "news_negative": negative, "news_ids": sorted(set(used))}})
        except (ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
            invalid[symbol] = str(exc)
    return ranked_result(ranked, invalid, len(candidates))


def _extension(data_dir, fields, first, last, cutoff, *, config_id=None):
    """Discover standardized timeseries fields; never bind a vendor SDK."""
    from app.services.ext_data import ExtConfigStore
    configs = [cfg for cfg in ExtConfigStore(data_dir).load_all() if cfg.mode == "timeseries"
               and set(fields) <= {field.name for field in cfg.fields}
               and (config_id is None or cfg.id == config_id)]
    if len(configs) != 1:
        raise ValueError(f"S03标准化扩展数据源缺失或不唯一: {','.join(fields)}")
    cfg = configs[0]
    frames, evidence = [], []
    for path in sorted((data_dir / "ext_data" / cfg.id / "timeseries").glob("date=*/part.parquet")):
        if not str(first) <= path.parent.name[5:] <= str(last):
            continue
        before = path.stat()
        frame = pl.read_parquet(path)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if before.st_mtime_ns != path.stat().st_mtime_ns:
            raise ValueError("S03扩展分区在读取期间变化")
        if "observed_at" not in frame.columns:
            raise ValueError(f"S03扩展分区缺观察时点: {cfg.id}/{path.parent.name}")
        frame = frame.filter(pl.Series([
            value is not None and as_cn_datetime(datetime.fromisoformat(str(value))) <= cutoff
            for value in frame["observed_at"]]))
        frames.append(frame)
        evidence.append({"partition": path.parent.name, "sha256": digest})
    if not evidence:
        raise ValueError(f"S03标准化扩展数据尚未就绪: {cfg.id}")
    frame = pl.concat(frames, how="diagonal_relaxed")
    if frame.is_empty():
        raise ValueError(f"S03标准化扩展数据在本次时刻无有效观察: {cfg.id}")
    return frame, {"config_id": cfg.id, "partitions": evidence}


def enrich_candidates(engine, context, ranked, overrides):
    """Retain the saved strategy's optional next-session metrics and fixed rank."""
    from app.services.selection_clock import S03_ID
    from app.strategy.engine import _sanitize
    strategy = engine.get(S03_ID)
    enrich = getattr(strategy, "enrich_selection_fn", None)
    if enrich is None or not ranked["rows"]:
        return ranked
    rows = pl.DataFrame(ranked["rows"], infer_schema_length=None)
    enriched = enrich(rows.clone(), engine.resolve_params(strategy, None, overrides),
                      daily=context.daily_history, as_of=context.as_of)
    if (not isinstance(enriched, pl.DataFrame) or "symbol" not in enriched.columns
            or enriched.height != rows.height or enriched["symbol"].n_unique() != rows.height
            or set(enriched["symbol"]) != set(rows["symbol"])):
        raise ValueError("S03预选补充指标不得增删或重复选股成员")
    extra = [name for name in enriched.columns if name not in rows.columns]
    if extra:
        ranked = {**ranked, "rows": _sanitize(rows.join(
            enriched.select("symbol", *extra), on="symbol", how="left",
            maintain_order="left").to_dicts())}
    return ranked


def compute_selection(engine, service, source, target, overrides, dependency, now):
    from app.services.daily_pool_schedule import scheduled_for
    from app.services.financial_sync import latest_financial_revision_meta, read_financials_as_of
    from app.services.related_market_data import DATASET_IDS
    from app.services.selection_clock import S03_ID
    if not dependency:
        raise ValueError("等待S02的20:00成功预选归档")
    validated = freeze_source(dependency.get("snapshot"), source, target)
    if dependency != validated:
        raise ValueError("S03冻结S02依赖摘要不匹配")
    snapshot = validated["snapshot"]
    if not snapshot["rows"]:
        ranked = ranked_result([], {}, 0)
        provenance = {}
    else:
        context = service.build_selection_context(
            engine, source, target, [S03_ID], overrides_map=overrides, history_bars=65)
        daily = context.daily_history
        data_dir = service.repo.store.data_dir
        cutoff = as_cn_datetime(now)
        sessions = sorted(set(daily["date"].to_list()))[-20:]
        datasets = engine.get(S03_ID).meta.get("selection_data_sources") or {}
        flows, flow_meta = _extension(data_dir, ["super_large_net_inflow", "large_net_inflow"],
                                      sessions[0], source, cutoff,
                                      config_id=datasets.get("funds", DATASET_IDS["sina_money_flow"]))
        notices, news_meta = _extension(data_dir, ["title", "announcement_id"],
                                        source - timedelta(days=10), source - timedelta(days=1), cutoff,
                                        config_id=datasets.get("news", DATASET_IDS["announcements"]))
        financial = {key: read_financials_as_of(data_dir, key, cutoff)
                     for key in ("metrics", "income", "cash_flow")}
        current = daily.filter(pl.col("date") == source)
        prior = daily.sort("date").with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1).alias("_return"))
        returns = prior.filter((pl.col("date") == source) & pl.col("_return").is_finite())
        breadth = float((returns["_return"] > 0).mean()) if returns.height else None
        ranked = rank_candidates(snapshot, daily, financial, flows, {"breadth": breadth}, notices, source)
        ranked = enrich_candidates(engine, context, ranked, overrides.get(S03_ID, {}))
        from app.strategy.signal_price_suggestions import price_reference
        ranked["price_suggestion_reference"] = price_reference(context)
        # Candidate evidence is independent of the full strategy scope.
        ranked["coverage"]["market_available"] = current.height
        provenance = {"funds": flow_meta, "news": news_meta,
                      "financials": {key: latest_financial_revision_meta(data_dir, key, cutoff)
                                     for key in financial},
                      "observed_cutoff": cutoff.isoformat(), "daily_history_bars": 65,
                      "news_policy": "T日前10个自然日；发行人名称匹配；T日公告不使用"}
    return {**ranked, "as_of": str(target), "source_date": str(source),
            "generated_at": as_cn_datetime(now).isoformat(),
            "scheduled_for": scheduled_for(source, "21:00"),
            "source_selection": validated, "scoring_provenance": provenance,
            **({"price_suggestion_reference": snapshot["price_suggestion_reference"]}
               if snapshot.get("price_suggestion_reference") else {})}
