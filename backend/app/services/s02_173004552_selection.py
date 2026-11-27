"""Freeze S02's four dimension ranks over the existing complete selection."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

import polars as pl

from app.strategy.s02_173004552 import POLICY, route, sessions_until

WARMUP = date(2025, 10, 1)
WEIGHTS = {"tech": .40, "fund": .30, "emotion": .25, "fundamental": .05}


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def scale(value, low, high):
    # The research gives missing subcomponents a neutral 50.
    return 100 * min(1, max(0, (value - low) / (high - low))) if finite(value) else 50.


def _daily_input(daily, source):
    cols = ("symbol", "date", "close", "raw_close", "high", "low", "amount")
    if not set(cols) <= set(daily.columns):
        raise ValueError("S02四维评分缺少完整日线字段")
    daily = (daily.select(cols).with_columns(pl.col("date").cast(pl.Date))
             .filter((pl.col("date") <= source) & pl.col("symbol").str.contains(r"^(60|00)")))
    bad = set(daily.group_by("symbol", "date").len().filter(pl.col("len") > 1)["symbol"])
    bad.update(daily.filter(
        ~pl.col("close").is_finite().fill_null(False) | (pl.col("close") <= 0)
    )["symbol"])
    return daily.filter(~pl.col("symbol").is_in(sorted(bad))).sort("symbol", "date"), bad


def _features(daily):
    close, amount = pl.col("close"), pl.col("amount")
    return daily.with_columns(
                 *(close.rolling_mean(n).over("symbol").alias(f"ma{n}") for n in (5, 10, 20, 60)),
                 (close / close.shift(20).over("symbol") - 1).alias("mom20"),
                 (close / close.shift(1).over("symbol") - 1).alias("change"),
                 (close.ewm_mean(span=12, adjust=False).over("symbol")
                  - close.ewm_mean(span=26, adjust=False).over("symbol")).alias("dif"),
                 (amount / amount.rolling_mean(20).over("symbol")).alias("volume_ratio"),
             ).with_columns(
                 (pl.col("ma20") / pl.col("ma20").shift(5).over("symbol") - 1).alias("slope"),
                 ((pl.col("dif") - pl.col("dif").ewm_mean(span=9, adjust=False).over("symbol"))
                  / close).alias("macd"),
                 (close / pl.col("ma20") - 1).abs().alias("bias"),
             )


def _group_reports(financial, source):
    reports = {}
    for table in ("metrics", "income", "cash_flow"):
        data = financial.get(table)
        if data is None or data.is_empty():
            reports[table] = {}
            continue
        data = data.with_columns(pl.col("announce_date", "period_end").cast(pl.Date))
        latest = (data.filter((pl.col("announce_date") < source) & (pl.col("period_end") < source))
                  .sort("period_end", "announce_date", descending=True).unique("symbol", keep="first"))
        reports[table] = {row["symbol"]: row for row in latest.to_dicts()}
    return reports


def _fundamental(reports, symbol, raw):
    metrics, income, cash = (reports[key].get(symbol, {}) for key in ("metrics", "income", "cash_flow"))
    profit, ocf = income.get("net_income"), cash.get("net_operating_cash_flow")
    quality = (ocf / profit if finite(profit) and profit > 0 and finite(ocf)
               and income.get("period_end") == cash.get("period_end") else None)
    eps = metrics.get("eps_basic")
    pe = raw / (eps * 12 / metrics["period_end"].month) if finite(eps) and eps > 0 else None
    return (.30 * scale(metrics.get("net_income_yoy"), -20, 40)
            + .25 * scale(metrics.get("roe"), 0, 15) + .25 * scale(quality, 0, 1.5)
            + .10 * (100 - scale(metrics.get("debt_to_asset_ratio"), 30, 85))
            + .10 * (100 - scale(pe, 15, 60)))


def freeze_selection(snapshot, daily, flows, financial, day, *, include_missing_components=False):
    """Rank only eligible source rows. Keep the whole S02 list for S03."""
    days = sessions_until(day)
    source = days[-1]
    candidates = snapshot["rows"]
    symbols = [row["symbol"] for row in candidates]
    if (snapshot.get("as_of") != str(day) or snapshot.get("source_date") != str(source)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("S02四维预选日期不一致或候选重复")
    daily, bad = _daily_input(daily, source)
    close = pl.col("close")
    market = (daily.select("symbol", "date", "close").with_columns(
        (close / close.shift(1).over("symbol") - 1).alias("change"),
        close.rolling_mean(20).over("symbol").alias("ma20"),
    ).filter(pl.col("date").is_in(days)).group_by("date").agg(
        (pl.col("change") > 0).mean().alias("up"),
        (pl.col("close") > pl.col("ma20")).mean().alias("above_ma20"),
    ).sort("date").with_columns(pl.col("date").cast(pl.String)).to_dicts())
    branch = route(market, day)
    scoped = _features(daily.filter(pl.col("symbol").is_in(symbols))).filter(pl.col("date").is_in(days[-20:]))
    by_stock = {key[0]: {row["date"]: row for row in frame.to_dicts()}
                for key, frame in scoped.partition_by("symbol", as_dict=True).items()}
    flow_map = {}
    if not flows.is_empty():
        flow_map = {(row["symbol"], row["date"]): row for row in flows.with_columns(
            pl.col("date").cast(pl.Date)).filter(pl.col("symbol").is_in(symbols))
            .unique(["symbol", "date"], keep="last").to_dicts()}
    reports = _group_reports(financial, source)
    ranked, invalid, missing = {}, {}, {}
    for candidate in candidates:
        symbol = candidate["symbol"]
        history = by_stock.get(symbol, {})
        row = history.get(source, {})
        raw, reference = row.get("raw_close"), candidate.get("reference_close")
        if (symbol in bad or not finite(raw) or raw <= 0 or not finite(reference)
                or abs(raw - reference) > .0050001):
            invalid[symbol] = "S02评分日线缺失、重复、价格异常或与冻结昨收不一致"
            continue
        pairs = zip((row["close"], row["ma5"], row["ma10"], row["ma20"]),
                    (row["ma5"], row["ma10"], row["ma20"], row["ma60"]), strict=True)
        tech = (.30 * sum(25 * (a > b) if finite(a) and finite(b) else 12.5 for a, b in pairs)
                + .20 * scale(row["slope"], -.01, .03) + .20 * scale(row["mom20"], -.05, .2)
                + .15 * scale(row["macd"], -.003, .006) + .15 * (100 - scale(row["bias"], .04, .18)))
        ratios = []
        for stamp in days[-20:]:
            flow = flow_map.get((symbol, stamp), {})
            a, b, amount = flow.get("super_large_net_inflow"), flow.get("large_net_inflow"), history.get(stamp, {}).get("amount")
            ratios.append((a + b) / amount if all(map(finite, (a, b, amount))) and amount > 0 else None)
        f5, f20 = [x for x in ratios[-5:] if x is not None], [x for x in ratios if x is not None]
        fund = (.35 * scale(ratios[-1], -.1, .15)
                + .30 * scale(sum(f5) / 5 if len(f5) == 5 else None, -.05, .10)
                + .20 * scale(sum(f20) / 20 if len(f20) == 20 else None, -.03, .06)
                + .15 * scale(sum(x > 0 for x in f5) / 5 if len(f5) == 5 else None, .2, .8))
        upper, lower, close = row["high"], row["low"], row["close"]
        if not finite(upper) or not finite(lower) or not 0 < lower <= close <= upper:
            invalid[symbol] = "S02评分源日日线高低价异常"
            continue
        position = (close - lower) / (upper - lower) if upper > lower else .5
        emotion = (.25 * scale(row["change"], 0, .06) + .30 * scale(position, 0, 1)
                   + .25 * scale(row["volume_ratio"], .7, 2) + .20 * scale(market[-1]["up"], 0, 1))
        if include_missing_components:
            absent = [key for key in ("ma5", "ma10", "ma20", "ma60", "slope", "mom20",
                                      "macd", "bias", "change", "volume_ratio") if not finite(row.get(key))]
            absent += [f"fund:{stamp}" for stamp, ratio in zip(days[-20:], ratios, strict=True)
                       if ratio is None]
            metrics = reports["metrics"].get(symbol, {})
            absent += [f"financial:{key}" for key in
                       ("net_income_yoy", "roe", "debt_to_asset_ratio", "eps_basic")
                       if not finite(metrics.get(key))]
            income, cash = reports["income"].get(symbol, {}), reports["cash_flow"].get(symbol, {})
            if (not finite(income.get("net_income")) or not finite(cash.get("net_operating_cash_flow"))
                    or income.get("period_end") != cash.get("period_end")):
                absent.append("financial:cash_flow_quality")
            if absent:
                missing[symbol] = absent
        scores = dict(zip(WEIGHTS, (tech, fund, emotion, _fundamental(reports, symbol, raw)), strict=True))
        ranked[symbol] = {**{f"s02_{k}_score": v for k, v in scores.items()},
                          "s02_score": sum(value * WEIGHTS[k] for k, value in scores.items())}
    if candidates and not ranked:
        raise ValueError("S02有预选候选但全部无法进行四维评分")
    top = {symbol: {**ranked[symbol], "s02_rank": rank} for rank, symbol in enumerate(
        sorted(ranked, key=lambda s: (-ranked[s]["s02_score"], s))[:50], 1)}
    return {**snapshot, "entry_routing": POLICY, "entry_branch": branch, "market_breadth": market,
            "rows": [{**row, **top.get(row["symbol"], {})} for row in candidates],
            "warnings": [*snapshot.get("warnings", []),
                         *([f"S02四维评分隔离{len(invalid)}只异常股票"] if invalid else [])],
            "score_coverage": {"candidate_total": len(candidates), "scorable_total": len(ranked),
                               "ranked_total": len(top), "invalid_symbols": invalid,
                               **({"missing_components": missing} if include_missing_components else {})}}


def distribution_exclusions(rows, notices, day):
    """Bind recent implementation notices to their actual issuers."""
    def issuer(name):
        return re.sub(r"[^A-Za-z0-9\u3400-\u9fff]", "", str(name or "")).lower()

    names = {row["symbol"]: issuer(row.get("name")) for row in rows}
    blocked = set()
    for row in notices.to_dicts():
        title = str(row.get("title") or "")
        symbol = row.get("symbol")
        if (not names.get(symbol) or issuer(re.split("[:\uff1a]", title, maxsplit=1)[0]) != names[symbol]
                or not re.search(r"(权益分派|权益分配|利润分配|分红派息|派息).*实施", title)):
            continue
        try:
            published = datetime.fromisoformat(str(row.get("display_time"))).date()
        except ValueError:
            continue
        if 0 < (day - published).days <= 14:
            blocked.add(symbol)
    return blocked


def compute_selection(repo, snapshot, source, day, observed, *, include_missing_components=False):
    """Read completed public partitions and observed funds and finance."""
    from app.services.financial_sync import latest_financial_revision_meta, read_financials_as_of
    from app.services.related_market_data import DATASET_IDS
    from app.services.s03_selection import _extension

    root = repo.store.data_dir
    cols = ("symbol", "date", "close", "raw_close", "high", "low", "amount")
    paths = [path for path in sorted((root / "kline_daily_enriched").glob("date=*/part.parquet"))
             if str(WARMUP) <= path.parent.name[5:] <= str(source)]
    if not paths:
        raise ValueError("S02缺少冻结评分日线分区")
    daily = pl.scan_parquet(paths, missing_columns="insert").select(cols).collect()
    flows, financial, provenance = pl.DataFrame(), {}, {}
    if snapshot["rows"]:
        flows, provenance["funds"] = _extension(root, ["super_large_net_inflow", "large_net_inflow"],
            sessions_until(day)[-20], source, observed, config_id=DATASET_IDS["sina_money_flow"])
        financial = {key: read_financials_as_of(root, key, observed)
                     for key in ("metrics", "income", "cash_flow")}
        provenance["financials"] = {key: latest_financial_revision_meta(root, key, observed) for key in financial}
    frozen = freeze_selection(
        snapshot, daily, flows, financial, day, include_missing_components=include_missing_components)
    ranked = [row for row in frozen["rows"] if row.get("s02_rank") is not None]
    if ranked:
        notices, provenance["distribution_notices"] = _extension(root, ["title", "announcement_id"],
            day - timedelta(days=14), source, observed, config_id=DATASET_IDS["announcements"])
        blocked = distribution_exclusions(ranked, notices, day)
        frozen["rows"] = [{**row, "s02_entry_exclusion": "recent_distribution_notice"}
                          if row["symbol"] in blocked else row for row in frozen["rows"]]
        frozen["score_coverage"]["excluded_for_entry"] = sorted(blocked)
    return {**frozen,
            "score_provenance": {**provenance, "observed_cutoff": observed.isoformat(),
                                 "daily_warmup": str(WARMUP), "daily_through": str(source)}}
