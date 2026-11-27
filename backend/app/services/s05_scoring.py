"""S05's independent four-dimension scoring over a completed source archive."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from itertools import pairwise

import polars as pl

from app.strategy.s02_173004552 import POLICY as ENTRY_POLICY
from app.strategy.s02_173004552 import route, sessions_until

POLICY = "s05_four_dimensions_40_30_25_5_v1"
IMPLEMENTATION = "s05_independent_four_dimensions_v1"
WEIGHTS = {"tech": .40, "fund": .30, "emotion": .25, "fundamental": .05}
WARMUP = date(2025, 10, 1)
DAILY_COLUMNS = ("symbol", "date", "close", "raw_close", "high", "low", "amount")


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _scale(value, low, high):
    """Missing subcomponents are neutral; valid values saturate at 0/100."""
    if not _finite(value):
        return 50.
    return min(1., max(0., (value - low) / (high - low))) * 100


def _daily(daily, source):
    if not set(DAILY_COLUMNS).issubset(daily.columns):
        raise ValueError("S05四维评分缺少完整日线字段")
    panel = (daily.select(DAILY_COLUMNS).with_columns(pl.col("date").cast(pl.Date))
             .filter((pl.col("date") <= source) & pl.col("symbol").str.contains(r"^(60|00)")))
    bad = set(panel.group_by("symbol", "date").len().filter(pl.col("len") > 1)["symbol"])
    bad.update(panel.filter(
        ~pl.col("close").is_finite().fill_null(False) | (pl.col("close") <= 0))["symbol"])
    return panel.filter(~pl.col("symbol").is_in(sorted(bad))).sort("symbol", "date"), bad


def _breadth(panel, days):
    close = pl.col("close")
    return (panel.with_columns(
        (close / close.shift(1).over("symbol") - 1).alias("change"),
        close.rolling_mean(20).over("symbol").alias("ma20"),
    ).filter(pl.col("date").is_in(days)).group_by("date").agg(
        (pl.col("change") > 0).mean().alias("up"),
        (close > pl.col("ma20")).mean().alias("above_ma20"),
    ).sort("date").with_columns(pl.col("date").cast(pl.String)).to_dicts())


def _features(panel):
    close = pl.col("close")
    return panel.with_columns(
        *(close.rolling_mean(n).over("symbol").alias(f"ma{n}") for n in (5, 10, 20, 60)),
        (close / close.shift(20).over("symbol") - 1).alias("mom20"),
        (close / close.shift(1).over("symbol") - 1).alias("change"),
        (close.ewm_mean(span=12, adjust=False).over("symbol")
         - close.ewm_mean(span=26, adjust=False).over("symbol")).alias("dif"),
        (pl.col("amount") / pl.col("amount").rolling_mean(20).over("symbol")).alias("volume_ratio"),
    ).with_columns(
        (pl.col("ma20") / pl.col("ma20").shift(5).over("symbol") - 1).alias("slope"),
        ((pl.col("dif") - pl.col("dif").ewm_mean(span=9, adjust=False).over("symbol"))
         / close).alias("macd"),
        (close / pl.col("ma20") - 1).abs().alias("bias"),
    )


def _reports(financial, source):
    result = {}
    for table in ("metrics", "income", "cash_flow"):
        frame = financial.get(table)
        if frame is None or frame.is_empty():
            result[table] = {}
            continue
        frame = frame.with_columns(pl.col("announce_date", "period_end").cast(pl.Date))
        public = (frame.filter((pl.col("announce_date") < source) & (pl.col("period_end") < source))
                  .sort("period_end", "announce_date", descending=True).unique("symbol", keep="first"))
        result[table] = {row["symbol"]: row for row in public.to_dicts()}
    return result


def _technical(row):
    chain = ("close", "ma5", "ma10", "ma20", "ma60")
    alignment = sum(
        25 * (row[a] > row[b]) if _finite(row[a]) and _finite(row[b]) else 12.5
        for a, b in pairwise(chain))
    return (.30 * alignment + .20 * _scale(row["slope"], -.01, .03)
            + .20 * _scale(row["mom20"], -.05, .20)
            + .15 * _scale(row["macd"], -.003, .006)
            + .15 * (100 - _scale(row["bias"], .04, .18)))


def _funding(ratios):
    five = [value for value in ratios[-5:] if value is not None]
    twenty = [value for value in ratios if value is not None]
    return (.35 * _scale(ratios[-1], -.10, .15)
            + .30 * _scale(sum(five) / 5 if len(five) == 5 else None, -.05, .10)
            + .20 * _scale(sum(twenty) / 20 if len(twenty) == 20 else None, -.03, .06)
            + .15 * _scale(sum(value > 0 for value in five) / 5 if len(five) == 5 else None, .20, .80))


def _emotion(row, up):
    span = row["high"] - row["low"]
    position = (row["close"] - row["low"]) / span if span > 0 else .5
    return (.25 * _scale(row["change"], 0, .06) + .30 * _scale(position, 0, 1)
            + .25 * _scale(row["volume_ratio"], .7, 2) + .20 * _scale(up, 0, 1))


def _fundamentals(reports, symbol, raw):
    metrics, income, cash = (reports[key].get(symbol, {}) for key in ("metrics", "income", "cash_flow"))
    profit, operating = income.get("net_income"), cash.get("net_operating_cash_flow")
    quality = (operating / profit if _finite(profit) and profit > 0 and _finite(operating)
               and income.get("period_end") == cash.get("period_end") else None)
    eps = metrics.get("eps_basic")
    pe = raw / (eps * 12 / metrics["period_end"].month) if _finite(eps) and eps > 0 else None
    return (.30 * _scale(metrics.get("net_income_yoy"), -20, 40)
            + .25 * _scale(metrics.get("roe"), 0, 15) + .25 * _scale(quality, 0, 1.5)
            + .10 * (100 - _scale(metrics.get("debt_to_asset_ratio"), 30, 85))
            + .10 * (100 - _scale(pe, 15, 60)))


def _missing(row, ratios, days, reports, symbol):
    absent = [key for key in ("ma5", "ma10", "ma20", "ma60", "slope", "mom20",
                              "macd", "bias", "change", "volume_ratio") if not _finite(row.get(key))]
    absent += [f"fund:{day}" for day, ratio in zip(days[-20:], ratios, strict=True) if ratio is None]
    metrics, income, cash = (reports[key].get(symbol, {}) for key in ("metrics", "income", "cash_flow"))
    absent += [f"financial:{key}" for key in ("net_income_yoy", "roe", "debt_to_asset_ratio", "eps_basic")
               if not _finite(metrics.get(key))]
    if (not _finite(income.get("net_income")) or not _finite(cash.get("net_operating_cash_flow"))
            or income.get("period_end") != cash.get("period_end")):
        absent.append("financial:cash_flow_quality")
    return absent


def freeze_selection(snapshot, daily, flows, financial, day, *, include_entry_route=True):
    """Own the four scores and top 50 while retaining routed archive field names."""
    days = sessions_until(day)
    source = days[-1]
    candidates = snapshot["rows"]
    symbols = [row["symbol"] for row in candidates]
    if (snapshot.get("as_of") != str(day) or snapshot.get("source_date") != str(source)
            or len(set(symbols)) != len(symbols)):
        raise ValueError("S05四维预选日期不一致或候选重复")
    panel, bad = _daily(daily, source)
    market = _breadth(panel, days)
    branch = route(market, day) if include_entry_route else None
    scoped = _features(panel.filter(pl.col("symbol").is_in(symbols))).filter(pl.col("date").is_in(days[-20:]))
    history = {key[0]: {row["date"]: row for row in frame.to_dicts()}
               for key, frame in scoped.partition_by("symbol", as_dict=True).items()}
    flow_map = {}
    if not flows.is_empty():
        frame = (flows.with_columns(pl.col("date").cast(pl.Date)).filter(pl.col("symbol").is_in(symbols))
                 .unique(["symbol", "date"], keep="last"))
        flow_map = {(row["symbol"], row["date"]): row for row in frame.to_dicts()}
    reports = _reports(financial, source)
    scored, invalid, missing = {}, {}, {}
    for candidate in candidates:
        symbol = candidate["symbol"]
        stock = history.get(symbol, {})
        row = stock.get(source, {})
        raw, reference = row.get("raw_close"), candidate.get("reference_close")
        if (symbol in bad or not _finite(raw) or raw <= 0 or not _finite(reference)
                or abs(raw - reference) > .0050001):
            invalid[symbol] = "S05评分日线缺失、重复、价格异常或与冻结昨收不一致"
            continue
        if (not _finite(row["high"]) or not _finite(row["low"])
                or not 0 < row["low"] <= row["close"] <= row["high"]):
            invalid[symbol] = "S05评分源日日线高低价异常"
            continue
        ratios = []
        for stamp in days[-20:]:
            flow = flow_map.get((symbol, stamp), {})
            large, extra = flow.get("large_net_inflow"), flow.get("super_large_net_inflow")
            amount = stock.get(stamp, {}).get("amount")
            ratios.append((large + extra) / amount
                          if all(_finite(value) for value in (large, extra, amount)) and amount > 0 else None)
        scores = dict(zip(WEIGHTS, (_technical(row), _funding(ratios), _emotion(row, market[-1]["up"]),
                                   _fundamentals(reports, symbol, raw)), strict=True))
        scored[symbol] = {**{f"s02_{key}_score": value for key, value in scores.items()},
                          "s02_score": sum(value * WEIGHTS[key] for key, value in scores.items())}
        absent = _missing(row, ratios, days, reports, symbol)
        if absent:
            missing[symbol] = absent
    if candidates and not scored:
        raise ValueError("S05有预选候选但全部无法进行四维评分")
    ordered = sorted(scored, key=lambda symbol: (-scored[symbol]["s02_score"], symbol))[:50]
    originals = {row["symbol"]: row for row in candidates}
    coverage = {"candidate_total": len(candidates), "scorable_total": len(scored), "ranked_total": len(ordered),
                "invalid_symbols": invalid, "missing_components": missing}
    return {
        **snapshot, "market_breadth": market,
        **({"entry_routing": ENTRY_POLICY, "entry_branch": branch} if include_entry_route else {}),
        "rows": [{**originals[symbol], **scored[symbol], "s02_rank": rank}
                 for rank, symbol in enumerate(ordered, 1)],
        "warnings": [*snapshot.get("warnings", []),
                     *([f"S05四维评分隔离{len(invalid)}只异常股票"] if invalid else [])],
        "scoring_policy": POLICY, "scoring_weights": WEIGHTS, "score_coverage": coverage,
        "coverage": {**(snapshot.get("coverage") or {}), **coverage, "schema": POLICY, "selected_total": len(ordered)},
    }


def distribution_exclusions(rows, notices, day):
    def normalize(name):
        return re.sub(r"[^A-Za-z0-9\u3400-\u9fff]", "", str(name or "")).lower()

    issuers = {row["symbol"]: normalize(row.get("name")) for row in rows}
    blocked = set()
    for notice in notices.to_dicts():
        symbol, title = notice.get("symbol"), str(notice.get("title") or "")
        if (not issuers.get(symbol) or normalize(re.split("[:\uff1a]", title, maxsplit=1)[0]) != issuers[symbol]
                or not re.search(r"(权益分派|权益分配|利润分配|分红派息|派息).*实施", title)):
            continue
        try:
            published = datetime.fromisoformat(str(notice.get("display_time"))).date()
        except ValueError:
            continue
        if 0 < (day - published).days <= 14:
            blocked.add(symbol)
    return blocked


def compute_selection(repo, snapshot, source, day, observed, *, include_entry_route=True):
    """Use standard public partitions and observed extension/financial revisions."""
    from app.services.financial_sync import latest_financial_revision_meta, read_financials_as_of
    from app.services.related_market_data import DATASET_IDS
    from app.services.s03_selection import _extension

    root = repo.store.data_dir
    paths = [path for path in sorted((root / "kline_daily_enriched").glob("date=*/part.parquet"))
             if str(WARMUP) <= path.parent.name[5:] <= str(source)]
    if not paths:
        raise ValueError("S05缺少冻结评分日线分区")
    daily = pl.scan_parquet(paths, missing_columns="insert").select(DAILY_COLUMNS).collect()
    flows, financial, provenance = pl.DataFrame(), {}, {}
    if snapshot["rows"]:
        flows, provenance["funds"] = _extension(
            root, ["super_large_net_inflow", "large_net_inflow"], sessions_until(day)[-20],
            source, observed, config_id=DATASET_IDS["sina_money_flow"])
        for key in ("metrics", "income", "cash_flow"):
            financial[key] = read_financials_as_of(root, key, observed)
        provenance["financials"] = {
            key: latest_financial_revision_meta(root, key, observed) for key in financial}
    frozen = freeze_selection(snapshot, daily, flows, financial, day, include_entry_route=include_entry_route)
    if frozen["rows"]:
        notices, provenance["distribution_notices"] = _extension(
            root, ["title", "announcement_id"], day - timedelta(days=14), source, observed,
            config_id=DATASET_IDS["announcements"])
        blocked = distribution_exclusions(frozen["rows"], notices, day)
        frozen["rows"] = [{**row, "s02_entry_exclusion": "recent_distribution_notice"}
                          if row["symbol"] in blocked else row for row in frozen["rows"]]
        frozen["score_coverage"]["excluded_for_entry"] = sorted(blocked)
    return {**frozen, "score_provenance": {
        **provenance, "scoring_implementation": IMPLEMENTATION, "observed_cutoff": observed.isoformat(),
        "daily_warmup": str(WARMUP), "daily_through": str(source)}}
